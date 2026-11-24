"""Partial refresh, ETag / 304 on the poll endpoints, and server-sent events for the chats.
Named test_zz_ so it runs after the tests that count open games."""
import asyncio
import json

import pytest
from sqlalchemy import select

from app.db.models import ChatMessage, Role, User
from app.db.session import session_scope
from app.services import realtime
from app.core.errors import RateLimited
from tests.test_web import login
from tests.test_zz_chat_store import JSON, _cleanup, _csrf, _setup

CONSOLE = {"Accept": "text/html"}


# ------------------------------------------------------------------ ETag / 304
def test_live_endpoint_sends_etag_and_answers_304_when_unchanged():
    name, event_id, _ = _setup()
    try:
        c, _ = login(name)
        first = c.get("/_live/lobby", headers=JSON)
        etag = first.headers["etag"]
        assert first.status_code == 200 and "events" in first.json()
        again = c.get("/_live/lobby", headers={**JSON, "If-None-Match": etag})
        assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == etag
        # a proxy may append its encoding to the validator, and weak comparison must still match
        proxied = c.get("/_live/lobby", headers={**JSON, "If-None-Match": f'W/{etag[:-1]}-gzip"'})
        assert proxied.status_code == 304
        stale = c.get("/_live/lobby", headers={**JSON, "If-None-Match": '"nope"'})
        assert stale.status_code == 200
        # the old ?rev= handshake still works for pages that loaded the previous script
        rev = first.json()["rev"]
        assert c.get(f"/_live/lobby?rev={rev}", headers=JSON).json()["same"] is True
    finally:
        _cleanup(event_id)


def test_staff_chat_poll_is_conditional_and_a_new_message_breaks_the_match():
    a, _ = login("admin")
    first = a.get("/chat/messages?after=0", headers=JSON)
    etag = first.headers["etag"]
    last = max([m["id"] for m in first.json()["messages"]] or [0])
    assert a.get(f"/chat/messages?after={last}", headers={**JSON, "If-None-Match": etag}).status_code in (200, 304)
    quiet = a.get(f"/chat/messages?after={last}", headers=JSON)
    assert a.get(f"/chat/messages?after={last}", headers={**JSON, "If-None-Match": quiet.headers["etag"]}).status_code == 304
    a.post("/chat/send", headers={"X-CSRF-Token": _csrf(a), **JSON}, json={"body": "etag-test-line"})
    fresh = a.get(f"/chat/messages?after={last}", headers={**JSON, "If-None-Match": quiet.headers["etag"]})
    assert fresh.status_code == 200 and any(m["body"] == "etag-test-line" for m in fresh.json()["messages"])


def test_fight_chat_poll_is_conditional():
    name, event_id, (fight_id, _) = _setup()
    try:
        c, _ = login(name)
        r = c.get(f"/fights/{fight_id}/chat", headers=JSON)
        assert r.status_code == 200 and r.headers["etag"]
        assert c.get(f"/fights/{fight_id}/chat", headers={**JSON, "If-None-Match": r.headers["etag"]}).status_code == 304
    finally:
        _cleanup(event_id)


# ------------------------------------------------------------------ partial page refresh
def test_region_fragment_is_just_the_box_with_etag_and_no_cookies():
    name, event_id, _ = _setup()
    try:
        c, _ = login(name)
        c.get(f"/events/{event_id}")                                    # gets the CSRF cookie
        page = c.get(f"/events/{event_id}", headers=CONSOLE)
        assert "<html" in page.text and 'data-region="event-body"' in page.text
        frag = c.get(f"/events/{event_id}", headers={**CONSOLE, "X-Fragment": "event-body"})
        assert frag.status_code == 200 and frag.headers["x-fragment"] == "event-body"
        assert "<html" not in frag.text and "<!--region" not in frag.text and "card-head" in frag.text
        assert len(frag.text) < len(page.text)
        assert "set-cookie" not in frag.headers                         # the page's own CSRF token must stay valid
        # the token inside the fragment is the page's current one, so its forms still post
        assert _csrf(c) in frag.text
        again = c.get(f"/events/{event_id}", headers={**CONSOLE, "X-Fragment": "event-body", "If-None-Match": frag.headers["etag"]})
        assert again.status_code == 304 and again.content == b""
    finally:
        _cleanup(event_id)


def test_unknown_region_falls_back_to_the_whole_page_without_the_fragment_header():
    name, event_id, _ = _setup()
    try:
        c, _ = login(name)
        r = c.get(f"/events/{event_id}", headers={**CONSOLE, "X-Fragment": "no-such-box"})
        assert r.status_code == 200 and "<html" in r.text and "x-fragment" not in r.headers
        bad = c.get(f"/events/{event_id}", headers={**CONSOLE, "X-Fragment": "../etc"})
        assert "<html" in bad.text
    finally:
        _cleanup(event_id)


def test_fragment_needs_the_same_sign_in_as_the_page():
    from fastapi.testclient import TestClient
    from app.main import app
    anon = TestClient(app, follow_redirects=False)
    r = anon.get("/events/1", headers={**CONSOLE, "X-Fragment": "event-body"})
    assert r.status_code in (302, 303, 401) and "event-body" not in r.text


def test_lobby_dashboard_and_fight_view_expose_their_regions():
    name, event_id, (fight_id, _) = _setup()
    try:
        c, _ = login(name)
        assert 'data-region="lobby"' in c.get("/events", follow_redirects=True).text
        assert 'data-region="fight-panel"' in c.get(f"/events/{event_id}?fight_id={fight_id}").text
        frag = c.get("/games/sabong/live?pick=1", headers={**CONSOLE, "X-Fragment": "lobby"})
        assert frag.headers["x-fragment"] == "lobby" and "data-live-lobby" in frag.text
    finally:
        _cleanup(event_id)


# ------------------------------------------------------------------ the hub
def test_publish_wakes_only_matching_subscribers_and_limits_hold():
    async def run():
        h = realtime.Hub()
        a = h.subscribe(1, {"chat"})
        b = h.subscribe(2, {"fight:9"})
        h.publish("chat")
        assert await a.wait(1.0) is True
        assert await b.wait(0.05) is False
        for _ in range(realtime.MAX_PER_USER - 1):
            h.subscribe(1, {"chat"})
        with pytest.raises(RateLimited):
            h.subscribe(1, {"chat"})
        h.unsubscribe(a)
        assert h.count_for(1) == realtime.MAX_PER_USER - 1
    asyncio.run(run())


def test_a_committed_chat_message_wakes_the_room_but_a_rolled_back_one_does_not():
    async def run():
        sub = realtime.hub.subscribe(987654, {realtime.chat_topic()})
        try:
            sub.clear()
            with session_scope() as db:
                admin = db.scalar(select(User).where(User.role == Role.ADMIN))
                db.add(ChatMessage(user_id=admin.id, body="wake-me"))
                db.flush()
                assert await sub.wait(0.1) is False          # flushed is not committed: nothing yet
            assert await sub.wait(1.0) is True               # committed: woken
            sub.clear()
            with pytest.raises(RuntimeError):
                with session_scope() as db:
                    admin = db.scalar(select(User).where(User.role == Role.ADMIN))
                    db.add(ChatMessage(user_id=admin.id, body="never-saved"))
                    db.flush()
                    raise RuntimeError("boom")
            assert await sub.wait(0.2) is False
        finally:
            realtime.hub.unsubscribe(sub)
    asyncio.run(run())


# ------------------------------------------------------------------ the stream endpoints
def _frames(text):
    out = []
    for block in text.split("\n\n"):
        ev = {"event": None, "data": None, "id": None}
        for line in block.splitlines():
            for key in ("event", "data", "id"):
                if line.startswith(key + ": "):
                    ev[key] = line[len(key) + 2:]
        if ev["event"]:
            out.append(ev)
    return out


def test_streams_refuse_anonymous_and_unauthorised_callers():
    from fastapi.testclient import TestClient
    from app.main import app
    anon = TestClient(app, follow_redirects=False)
    # a redirect to the sign-in page (what the app does for a page-style request) or a 401: never a stream
    for path in ("/chat/stream", "/fights/1/chat/stream"):
        r = anon.get(path)
        assert r.status_code in (303, 401) and "text/event-stream" not in r.headers.get("content-type", "")
    assert anon.get("/chat/stream", headers=JSON).status_code == 401


def test_staff_chat_stream_sends_state_then_bye_and_releases_its_slot(monkeypatch):
    monkeypatch.setattr(realtime, "MAX_AGE", 0.3)
    monkeypatch.setattr(realtime, "HEARTBEAT", 0.2)
    a, _ = login("admin")
    a.post("/chat/send", headers={"X-CSRF-Token": _csrf(a), **JSON}, json={"body": "stream-me"})
    before = realtime.hub.total
    with a.stream("GET", "/chat/stream?after=0") as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        assert "no-store" in r.headers["cache-control"] and r.headers["x-accel-buffering"] == "no"
        body = "".join(r.iter_text())
    frames = _frames(body)
    assert frames[0]["event"] == "state" and frames[-1]["event"] == "bye"
    state = json.loads(frames[0]["data"])
    assert any(m["body"] == "stream-me" for m in state["messages"]) and "room" in state and "on_air" in state
    assert frames[0]["id"] == str(max(m["id"] for m in state["messages"]))        # resume point for Last-Event-ID
    assert realtime.hub.total == before


def test_stream_resumes_after_last_event_id(monkeypatch):
    monkeypatch.setattr(realtime, "MAX_AGE", 0.2)
    a, _ = login("admin")
    sent = a.post("/chat/send", headers={"X-CSRF-Token": _csrf(a), **JSON}, json={"body": "resume-me"}).json()["id"]
    with a.stream("GET", "/chat/stream?after=0", headers={"Last-Event-ID": str(sent)}) as r:
        frames = _frames("".join(r.iter_text()))
    state = json.loads(frames[0]["data"])
    assert all(m["id"] > sent for m in state["messages"])


def test_fight_stream_delivers_history_and_the_server_ends_it_politely(monkeypatch):
    monkeypatch.setattr(realtime, "MAX_AGE", 0.3)
    name, event_id, (fight_id, _) = _setup()
    try:
        c, _ = login(name)
        c.post("/store/pass", headers={"X-CSRF-Token": _csrf(c)}, data={"idempotency_key": "rt-pass"})
        c.post(f"/fights/{fight_id}/chat/send", headers={"X-CSRF-Token": _csrf(c), **JSON}, json={"body": "hello stream"})
        with c.stream("GET", f"/fights/{fight_id}/chat/stream?after=0") as r:
            assert r.status_code == 200
            frames = _frames("".join(r.iter_text()))
        state = json.loads(frames[0]["data"])
        assert [m["body"] for m in state["messages"]] == ["hello stream"] and state["can_send"] is True
        assert frames[-1]["event"] == "bye"
    finally:
        _cleanup(event_id)
