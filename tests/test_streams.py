"""Admin-configurable livestream: link detection, SSRF guard, CSP, taking a source live, the admin page, the player."""
import json
import shutil
import subprocess

import pytest
from sqlalchemy import select

from app.core.errors import ValidationError
from app.core.settings_registry import SettingsStore
import datetime as dt
from types import SimpleNamespace

from app.db.models import Role, StreamAssignment, StreamSource, User
from app.db.session import session_scope
from app.services import streams
from tests.test_web import login


def _admin(db):
    return db.scalar(select(User).where(User.role == Role.ADMIN))


@pytest.fixture(autouse=True)
def _clean():
    yield
    with session_scope() as db:
        for row in db.scalars(select(StreamAssignment)).all():
            db.delete(row)
        for row in db.scalars(select(StreamSource)).all():
            db.delete(row)
        SettingsStore(db).set("stream.enabled", "true", _admin(db).id)
        SettingsStore(db).set("stream.allowed_hosts", "", _admin(db).id)
    streams.invalidate()


# ---------------------------------------------------------------- detection (the table in the plan, plus hostile input)

@pytest.mark.parametrize("raw,kind,extra", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?t=30", "youtube", "start=30"),
    ("https://www.youtube.com/live/AbCdEfGhIjK", "youtube", "AbCdEfGhIjK"),
    ("https://www.youtube.com/shorts/AbCdEfGhIjK", "youtube", "AbCdEfGhIjK"),
    ("https://cdn.example.com/live/main.m3u8?token=abc", "hls", None),
    ("https://cdn.example.com/replay.mp4", "file", None),
    ("https://cdn.example.com/clip.webm", "file", None),
    ("https://www.facebook.com/page/videos/1234567890/", "facebook", "plugins/video.php"),
    ("https://fb.watch/abc123/", "facebook", "plugins/video.php"),
    ('<iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ" width="1"></iframe>', "youtube", "dQw4w9WgXcQ"),
])
def test_detects_known_link_shapes(raw, kind, extra):
    d = streams.detect(raw)
    assert d.kind == kind
    if extra:
        assert extra in (d.embed_ref or d.url)
    if kind == "youtube":
        assert d.embed_ref.startswith("https://www.youtube-nocookie.com/embed/")


@pytest.mark.parametrize("raw", [
    "", "   ", "rtmp://host/live/key", "http://cdn.example.com/a.m3u8", "javascript:alert(1)", "data:text/html,hi",
    "https://127.0.0.1/a.m3u8", "https://192.168.1.5/a.mp4", "https://10.0.0.2/a.mp4", "https://localhost/a.mp4",
    "https://[::1]/a.m3u8", "https://user:pw@cdn.example.com/a.mp4", "https://cdn.example.com/a b.mp4",
    "https://www.youtube.com/watch?v=short", "https://www.facebook.com/somepage/", "https://random.example.org/page",
    "<div>not an iframe</div>", "https://metadata.internal/a.mp4", "https://169.254.169.254/a.mp4",
])
def test_rejects_hostile_or_unusable_links(raw):
    with pytest.raises(ValidationError):
        streams.detect(raw)


def test_iframe_paste_keeps_only_the_src():
    d = streams.detect('<iframe src="https://cdn.example.com/a.m3u8" onload="steal()" style="x"></iframe><script>1</script>')
    assert d.kind == "hls" and "onload" not in d.url and "script" not in d.url


def test_generic_embed_needs_the_host_on_the_allowlist():
    with pytest.raises(ValidationError):
        streams.detect("https://player.vimeo.com/video/123")
    assert streams.detect("https://player.vimeo.com/video/123", allowed_hosts={"player.vimeo.com"}).kind == "embed"


def test_rtmp_gets_help_text():
    with pytest.raises(ValidationError, match="HLS"):
        streams.detect("rtmp://host/live/key")


# ---------------------------------------------------------------- network safety

def test_dns_that_points_inside_is_refused(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.0.0.9", 443))])
    with pytest.raises(ValidationError, match="private"):
        streams.assert_public_host("sneaky.example.com")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443)),
                                                                (2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValidationError):            # one bad address among good ones is still refused
        streams.assert_public_host("mixed.example.com")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    assert streams.assert_public_host("ok.example.com") == ["93.184.216.34"]


def test_probe_never_raises_and_reports_plainly(monkeypatch):
    monkeypatch.setattr(streams, "_fetch", lambda *a, **k: (200, {"access-control-allow-origin": "*"},
                                                            b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\na.ts\n", "u"))
    r = streams.probe(streams.detect("https://cdn.example.com/a.m3u8"), site_origin="https://site.example")
    assert r["reachable"] and r["playable"] and r["live"] is True and r["cors"] is True
    monkeypatch.setattr(streams, "_fetch", lambda *a, **k: (200, {}, b"#EXTM3U\n#EXT-X-ENDLIST\n", "u"))
    r = streams.probe(streams.detect("https://cdn.example.com/a.m3u8"), site_origin="https://site.example")
    assert r["live"] is False and r["cors"] is False and any("Access-Control-Allow-Origin" in n for n in r["notes"])

    def boom(*a, **k):
        raise ValidationError("That address is on a private network. Use a public https link.")
    monkeypatch.setattr(streams, "_fetch", boom)
    assert any("private" in n for n in streams.probe(streams.detect("https://cdn.example.com/a.mp4"))["notes"])


# ---------------------------------------------------------------- CSP

BASE = ("default-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'self'")


def test_csp_is_unchanged_without_sources_and_never_gets_a_wildcard():
    assert streams.extend_csp(BASE, {"media": [], "frame": []}) == BASE
    out = streams.extend_csp(BASE, {"media": ["https://cdn.example.com"], "frame": ["https://www.youtube-nocookie.com"]})
    assert "connect-src 'self' https://cdn.example.com;" in out
    assert "media-src 'self' blob: https://cdn.example.com;" in out and "worker-src 'self' blob:;" in out
    assert "frame-src https://www.youtube-nocookie.com;" in out
    assert "*" not in out.replace("frame-ancestors", "") and "http:" not in out.replace("https:", "")
    assert "script-src 'self';" in out                  # scripts are never opened up


# ---------------------------------------------------------------- taking a source live

def test_take_stop_and_resolution():
    with session_scope() as db:
        admin, st = _admin(db), SettingsStore(db)
        assert streams.payload(db)["kind"] is None                     # nothing configured: the sample feed
        a = streams.create_source(db, admin, name="Main YT", raw_url="https://youtu.be/dQw4w9WgXcQ", settings=st, check_dns=False)
        b = streams.create_source(db, admin, name="Arena", raw_url="https://cdn.example.com/live/a.m3u8", settings=st, check_dns=False)
        assert a.default_mode == "live" and b.default_mode == "auto"
        assert streams.payload(db)["kind"] is None                     # saved is not live until it is taken
        streams.take(db, admin, b.id)
        p = streams.payload(db)
        assert p["kind"] == "hls" and p["src"].endswith("a.m3u8") and p["embed"] is None and p["name"] == "Arena"
        streams.take(db, admin, a.id)                                  # only one is ever live
        p = streams.payload(db)
        assert p["kind"] == "youtube" and p["embed"].startswith("https://www.youtube-nocookie.com/embed/") and p["src"] is None
        assert sum(1 for s in streams.list_sources(db) if s.is_active) == 1
        streams.set_enabled(db, admin, a.id, False)                    # turning the live one off drops it
        assert streams.payload(db)["kind"] is None
        with pytest.raises(ValidationError):
            streams.take(db, admin, a.id)
        streams.set_enabled(db, admin, a.id, True)
        streams.take(db, admin, a.id)
        st.set("stream.enabled", "false", admin.id)                    # master switch
        assert streams.payload(db)["kind"] is None
        st.set("stream.enabled", "true", admin.id)
        streams.stop(db, admin)
        assert streams.payload(db)["kind"] is None
        streams.delete(db, admin, a.id)


def test_file_source_is_always_a_replay():
    with session_scope() as db:
        r = streams.create_source(db, _admin(db), name="Replay", raw_url="https://cdn.example.com/r.mp4", default_mode="live",
                                  settings=SettingsStore(db), check_dns=False)
        assert r.default_mode == "replay"


# ---------------------------------------------------------------- admin page + live endpoint over HTTP

def _post(c, path, **data):
    tok = c.cookies.get("snx_csrf").split(".")[0]
    return c.post(path, data={"csrf_token": tok, **data}, headers={"X-CSRF-Token": tok}, follow_redirects=False)


def test_admin_streams_page_detect_save_take_and_viewers_poll(monkeypatch):
    monkeypatch.setattr(streams, "assert_public_host", lambda *a, **k: ["93.184.216.34"])
    monkeypatch.setattr(streams, "probe", lambda det, site_origin=None: {"reachable": True, "playable": True, "live": True,
                                                                         "embeddable": None, "cors": True, "notes": []})
    c, r = login("admin")
    assert r.status_code == 303
    page = c.get("/admin/streams")
    assert page.status_code == 200 and "Add a source" in page.text and "Nothing is live" in page.text
    assert 'href="/admin/streams"' in c.get("/admin").text                    # the tab is there

    bad = _post(c, "/admin/streams/detect", url="http://insecure.example.com/a.m3u8")
    assert bad.status_code == 200 and bad.json()["ok"] is False and "https" in bad.json()["error"]
    good = _post(c, "/admin/streams/detect", url="https://cdn.example.com/live/a.m3u8").json()
    assert good["ok"] and good["kind"] == "hls" and good["native"] is True and good["live"] is True

    saved = _post(c, "/admin/streams/create", name="Arena HLS", url="https://cdn.example.com/live/a.m3u8", mode="auto")
    assert saved.status_code == 303
    with session_scope() as db:
        sid = db.scalar(select(StreamSource.id).where(StreamSource.name == "Arena HLS"))
    assert sid and "Take live" in c.get("/admin/streams").text

    first = c.get("/_live/stream").json()
    assert first["kind"] is None                                              # saved but not taken
    assert _post(c, f"/admin/streams/{sid}/take").status_code == 303
    r1 = c.get("/_live/stream")
    assert r1.json()["kind"] == "hls" and r1.json()["src"] == "https://cdn.example.com/live/a.m3u8"
    assert c.get("/_live/stream", headers={"If-None-Match": r1.headers["ETag"]}).status_code == 304     # idle poll = headers only

    # the page's policy now allows exactly that host for media, and nothing else
    streams.refresh()
    csp = c.get("/dashboard").headers["content-security-policy"]
    assert "media-src 'self' blob: https://cdn.example.com;" in csp and "connect-src 'self' https://cdn.example.com;" in csp
    assert "script-src 'self';" in csp and "frame-src" not in csp

    assert _post(c, "/admin/streams/stop").status_code == 303
    assert c.get("/_live/stream").json()["kind"] is None


def test_streams_pages_need_the_permission():
    anon = login("nobody-here")[0] if False else None                         # (no account: use a bare client)
    from fastapi.testclient import TestClient
    from app.main import app
    bare = TestClient(app)
    assert bare.get("/admin/streams", follow_redirects=False).status_code in (302, 303, 401, 403)
    assert bare.get("/_live/stream", follow_redirects=False).status_code in (302, 303, 401, 403)
    assert anon is None


# ---------------------------------------------------------------- the player on the fight page

def test_fight_player_boots_with_the_taken_source():
    from tests.test_zz_live_sell import _cleanup, _setup
    ev_id, fight_id, _ = _setup()
    try:
        with session_scope() as db:
            admin = _admin(db)
            row = streams.create_source(db, admin, name="Cam", raw_url="https://cdn.example.com/live/a.m3u8",
                                        settings=SettingsStore(db), check_dns=False)
            streams.take(db, admin, row.id)
        c, _ = login("admin")
        html = c.get(f"/change_name?event_id={ev_id}&fight_id={fight_id}").text
        assert "data-vp-media" in html and 'data-live-url="/_live/stream/f' in html and "data-vp-mute" in html
        boot = html.split("data-stream='", 1)[1].split("'", 1)[0]
        assert json.loads(boot.replace("\\u0027", "'"))["kind"] == "hls"
        with session_scope() as db:
            streams.stop(db, _admin(db))
        html = c.get(f"/change_name?event_id={ev_id}&fight_id={fight_id}").text
        assert json.loads(html.split("data-stream='", 1)[1].split("'", 1)[0])["kind"] is None   # back to the sample feed
    finally:
        _cleanup(ev_id)


# ---------------------------------------------------------------- the vendored hls.js really loads as a plain script

@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_hls_bundle_defines_the_global_when_run_as_a_plain_script():
    from app.core import assets
    url = assets.asset_urls("js", "hls")[0]
    path = "app" + url if url.startswith("/static/") else url
    code = ("const vm=require('vm'),fs=require('fs');const ctx={};ctx.self=ctx;ctx.window=ctx;ctx.document={};"
            "vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);"
            "console.log(typeof ctx.Hls);")
    out = subprocess.run(["node", "-e", code, path], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "function", out.stderr[:300]


# =============================================================== Phase 2: what plays where

NOW = dt.datetime(2026, 11, 22, 12, 0, tzinfo=dt.timezone.utc)


def _a(i, scope, sid=None, start=None, end=None, active=True):
    return SimpleNamespace(id=i, scope_type=scope, scope_id=sid, active=active, starts_at=start, ends_at=end)


def test_most_specific_scope_wins_and_windows_are_respected():
    rows = [_a(1, "global"), _a(2, "event", 5), _a(3, "fight", 9), _a(4, "event", 6), _a(5, "fight", 10)]
    pick = streams.pick_assignment
    assert pick(rows, 5, 9, NOW).id == 3                      # game beats event beats everything
    assert pick(rows, 5, 8, NOW).id == 2                      # another game of that event: the event setting
    assert pick(rows, 7, 99, NOW).id == 1                     # an unrelated event: everything
    assert pick(rows, None, None, NOW).id == 1                # no scope known: everything
    assert pick([_a(1, "event", 5, active=False)], 5, None, NOW) is None
    future = NOW + dt.timedelta(hours=1)
    past = NOW - dt.timedelta(hours=1)
    assert pick([_a(1, "global"), _a(2, "global", start=future)], None, None, NOW).id == 1     # not started yet
    assert pick([_a(1, "global"), _a(2, "global", end=past)], None, None, NOW).id == 1         # already over
    assert pick([_a(1, "global"), _a(2, "global", start=past, end=future)], None, None, NOW).id == 2   # a window beats a standing one
    assert pick([_a(1, "global", end=NOW)], None, None, NOW) is None                            # the end is exclusive


def test_philippine_time_round_trip():
    assert streams.parse_local("2026-11-22T20:00") == dt.datetime(2026, 11, 22, 12, 0, tzinfo=dt.timezone.utc)
    assert streams.parse_local("") is None and streams.parse_local("  ") is None
    assert streams.fmt_local(dt.datetime(2026, 11, 22, 12, 0, tzinfo=dt.timezone.utc)) == "Nov 22, 08:00 PM"
    with pytest.raises(ValidationError):
        streams.parse_local("tomorrow")


def _mk(db, admin, name, url):
    return streams.create_source(db, admin, name=name, raw_url=url, settings=SettingsStore(db), check_dns=False)


def test_resolution_chain_failover_pin_slate_and_off():
    from tests.test_zz_live_sell import _cleanup, _setup
    ev_id, fight_id, _ = _setup()
    try:
        with session_scope() as db:
            admin = _admin(db)
            a = _mk(db, admin, "Primary", "https://cdn.example.com/live/a.m3u8")
            b = _mk(db, admin, "Backup", "https://cdn.example.com/replay.mp4")
            assert streams.payload(db, event_id=ev_id, fight_id=fight_id)["status"] == "sample"
            streams.create_assignment(db, admin, scope_type="global", scope_id=None, mode="live", chain=[a.id, b.id])
            streams.create_assignment(db, admin, scope_type="event", scope_id=ev_id, mode="replay", chain=[b.id])
            streams.create_assignment(db, admin, scope_type="fight", scope_id=fight_id, mode="standby", chain=[b.id])
            p = streams.payload(db, event_id=ev_id, fight_id=fight_id)
            assert (p["scope"], p["mode"], p["kind"], p["loop"], p["status"]) == ("fight", "standby", "file", True, "playing")
            p = streams.payload(db, event_id=ev_id, fight_id=None)
            assert (p["scope"], p["mode"]) == ("event", "replay")
            p = streams.payload(db, event_id=ev_id + 999, fight_id=None)
            assert (p["scope"], p["mode"], p["kind"], p["chain_position"]) == ("global", "auto", "hls", 0)   # HLS auto: live that turns into a replay

            # failover down the chain, a pin that ignores the check, then the slate
            a.health = "down"
            db.flush()
            p = streams.payload(db, event_id=ev_id + 999)
            assert p["kind"] == "file" and p["chain_position"] == 1 and p["name"] == "Backup"
            g = next(x for x in streams.list_assignments(db) if x.scope_type == "global")
            g.pinned_source_id = a.id
            assert streams.payload(db, event_id=ev_id + 999)["name"] == "Primary"          # forced even though it is down
            g.pinned_source_id = None
            b.health = "down"
            db.flush()
            p = streams.payload(db, event_id=ev_id + 999)
            assert p["status"] == "slate" and p["kind"] is None and p["message"]           # never a black box
            g.slate_message = "Back at 8 PM"
            assert streams.payload(db, event_id=ev_id + 999)["message"] == "Back at 8 PM"
            b.health = "unknown"
            b.enabled = False                                                              # switched off = unusable too
            db.flush()
            assert streams.payload(db, event_id=ev_id + 999)["status"] == "slate"
            b.enabled = True
            streams.create_assignment(db, admin, scope_type="event", scope_id=ev_id, mode="off", chain=[], slate_message="Intermission")
            p = streams.payload(db, event_id=ev_id)
            assert (p["status"], p["mode"], p["message"], p["kind"]) == ("off", "off", "Intermission", None)
            SettingsStore(db).set("stream.enabled", "false", admin.id)
            assert streams.payload(db, event_id=ev_id)["status"] == "sample"
    finally:
        _cleanup(ev_id)


def test_scheduled_windows_switch_by_themselves():
    with session_scope() as db:
        admin = _admin(db)
        clip, live = _mk(db, admin, "Clip", "https://cdn.example.com/standby.mp4"), _mk(db, admin, "Cam", "https://cdn.example.com/live/c.m3u8")
        t0 = NOW
        streams.create_assignment(db, admin, scope_type="global", scope_id=None, mode="standby", chain=[clip.id],
                                  ends_at=t0 + dt.timedelta(hours=1))
        streams.create_assignment(db, admin, scope_type="global", scope_id=None, mode="live", chain=[live.id],
                                  starts_at=t0 + dt.timedelta(hours=1), ends_at=t0 + dt.timedelta(hours=3))
        streams.create_assignment(db, admin, scope_type="global", scope_id=None, mode="replay", chain=[live.id],
                                  starts_at=t0 + dt.timedelta(hours=3))
        assert len(streams.list_assignments(db)) == 3                                   # windowed rows add up
        at = lambda h: streams.payload(db, now=t0 + dt.timedelta(hours=h))["mode"]       # noqa: E731
        assert (at(0.5), at(1), at(2), at(3), at(9)) == ("standby", "auto", "auto", "replay", "replay")
        assert streams.payload(db, now=t0 - dt.timedelta(hours=5))["mode"] == "standby"  # before: the first window's end only


def test_a_standing_setting_replaces_the_previous_standing_one():
    with session_scope() as db:
        admin = _admin(db)
        a, b = _mk(db, admin, "A", "https://cdn.example.com/a.m3u8"), _mk(db, admin, "B", "https://cdn.example.com/b.m3u8")
        streams.create_assignment(db, admin, scope_type="global", scope_id=None, mode="live", chain=[a.id])
        streams.create_assignment(db, admin, scope_type="global", scope_id=None, mode="live", chain=[b.id])
        rows = streams.list_assignments(db)
        assert len(rows) == 1 and streams.chain_ids(rows[0]) == [b.id]


def test_assignment_validation():
    from tests.test_zz_live_sell import _cleanup, _setup
    ev_id, fight_id, _ = _setup()
    try:
        with session_scope() as db:
            admin = _admin(db)
            a = _mk(db, admin, "A", "https://cdn.example.com/a.m3u8")
            off = _mk(db, admin, "Off", "https://cdn.example.com/o.m3u8")
            streams.set_enabled(db, admin, off.id, False)
            mk = lambda **kw: streams.create_assignment(db, admin, **{"scope_type": "global", "scope_id": None, "mode": "live", "chain": [a.id], **kw})  # noqa: E731
            for bad in (dict(chain=[]), dict(chain=[off.id]), dict(chain=[987654]), dict(mode="loop"), dict(scope_type="planet"),
                        dict(scope_type="event", scope_id=987654), dict(scope_type="fight", scope_id=987654),
                        dict(chain=list(range(1, 9))), dict(starts_at=NOW, ends_at=NOW)):
                with pytest.raises(ValidationError):
                    mk(**bad)
            assert mk(scope_type="fight", scope_id=fight_id).scope_id == fight_id
            assert mk(mode="off", chain=[]).chain == ""
    finally:
        _cleanup(ev_id)


def test_admin_assigns_per_event_and_per_game_and_viewers_poll_their_scope():
    from tests.test_zz_live_sell import _cleanup, _setup
    ev_id, fight_id, _ = _setup()
    try:
        with session_scope() as db:
            admin = _admin(db)
            a = _mk(db, admin, "Event cam", "https://cdn.example.com/live/e.m3u8").id
            b = _mk(db, admin, "Game clip", "https://cdn.example.com/g.mp4").id
            from app.db.models import Fight
            number = db.get(Fight, fight_id).number
        c, _ = login("admin")
        page = c.get("/admin/streams")
        assert page.status_code == 200 and "What plays where" in page.text and "Event cam" in page.text
        assert _post(c, "/admin/streams/assign", scope="event", event_id=ev_id, mode="live", s1=a).status_code == 303
        assert _post(c, "/admin/streams/assign", scope="fight", event_id=ev_id, game_number=number, mode="standby", s1=b).status_code == 303
        for bad in (dict(scope="fight", event_id=ev_id, game_number=9999, mode="live", s1=a),    # no such game
                    dict(scope="event", mode="live", s1=a),                                       # no event chosen
                    dict(scope="global", mode="live")):                                           # no source
            _post(c, "/admin/streams/assign", **bad)
        with session_scope() as db:
            assert len(streams.list_assignments(db)) == 2                                         # none of the bad ones was saved
        assert "Fight #" in c.get("/admin/streams").text and "Event:" in c.get("/admin/streams").text

        e = c.get(f"/_live/stream/e{ev_id}").json()
        g = c.get(f"/_live/stream/f{fight_id}").json()
        assert (e["scope"], e["kind"], e["status"]) == ("event", "hls", "playing")
        assert (g["scope"], g["kind"], g["mode"], g["loop"]) == ("fight", "file", "standby", True)
        assert c.get("/_live/stream/x1").status_code == 404 and c.get("/_live/stream/f999999").status_code == 404
        r = c.get(f"/_live/stream/f{fight_id}")
        assert c.get(f"/_live/stream/f{fight_id}", headers={"If-None-Match": r.headers["ETag"]}).status_code == 304

        html = c.get(f"/change_name?event_id={ev_id}&fight_id={fight_id}").text      # first paint already has the right video
        assert f'data-live-url="/_live/stream/f{fight_id}"' in html
        assert json.loads(html.split("data-stream='", 1)[1].split("'", 1)[0].replace("\\u0027", "'"))["mode"] == "standby"

        with session_scope() as db:
            gid = next(x.id for x in streams.list_assignments(db) if x.scope_type == "fight")
        assert _post(c, f"/admin/streams/assignments/{gid}/toggle", on="0").status_code == 303        # pause it: the event setting shows
        assert c.get(f"/_live/stream/f{fight_id}").json()["scope"] == "event"                          # (the live cache was cleared)
        assert _post(c, f"/admin/streams/assignments/{gid}/toggle", on="1").status_code == 303
        assert c.get(f"/_live/stream/f{fight_id}").json()["scope"] == "fight"
        assert _post(c, f"/admin/streams/assignments/{gid}/delete").status_code == 303
        assert c.get(f"/_live/stream/f{fight_id}").json()["scope"] == "event"
    finally:
        _cleanup(ev_id)


# ---------------------------------------------------------------- Phase 3: health checks, failover, impact, undo

NOW = dt.datetime(2026, 11, 22, 12, 0, tzinfo=dt.timezone.utc)
LIVE_PL = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:{seq}\n#EXTINF:4,\nseg{seq}.ts\n"


def test_parse_playlist_reads_what_matters():
    live = streams.parse_playlist(LIVE_PL.format(seq=120))
    assert live == {"ok": True, "master": False, "variant": None, "ended": False, "seq": 120}
    assert streams.parse_playlist(LIVE_PL.format(seq=3) + "#EXT-X-ENDLIST\n")["ended"] is True
    master = streams.parse_playlist("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nlow/index.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=2\nhi.m3u8\n")
    assert master["master"] and master["variant"] == "low/index.m3u8"
    assert streams.parse_playlist("<html>nope</html>")["ok"] is False and streams.parse_playlist("")["ok"] is False


def test_judge_needs_two_failures_to_go_down_and_one_success_to_recover():
    assert streams.judge("up", 0, False) == ("up", 1)                  # one blip is not an outage
    assert streams.judge("up", 1, False) == ("down", 2)
    assert streams.judge("down", 2, False) == ("down", 3)
    assert streams.judge("down", 3, True) == ("up", 0)
    assert streams.judge("unknown", 0, True) == ("up", 0)
    assert streams.judge("up", 1, None) == ("up", 1)                   # cannot tell (embed, third-party 5xx): no change
    assert streams.judge(None, 0, None) == ("unknown", 0)


def _fake_fetch(pages):
    """pages: {url: (status, body)} -> a stand-in for streams._fetch."""
    def fetch(url, **kw):
        status, body = pages[url] if url in pages else (404, "")
        return status, {"content-type": "video/mp4"}, body.encode(), url
    return fetch


def test_check_source_hls_follows_the_master_and_detects_a_stalled_stream(monkeypatch):
    src = SimpleNamespace(kind="hls", url="https://cdn.example.com/m.m3u8", media_seq=None, seq_changed_at=None)
    monkeypatch.setattr(streams, "_fetch", _fake_fetch({
        "https://cdn.example.com/m.m3u8": (200, "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv/live.m3u8\n"),
        "https://cdn.example.com/v/live.m3u8": (200, LIVE_PL.format(seq=50))}))
    ok, detail, seq = streams.check_source(src, NOW)
    assert (ok, seq) == (True, 50)
    src.media_seq, src.seq_changed_at = 50, NOW - dt.timedelta(seconds=20)                 # same number, 20 s: still fine
    assert streams.check_source(src, NOW)[0] is True
    src.seq_changed_at = NOW - dt.timedelta(seconds=streams.STALL_SECONDS + 5)              # same number, too long: stalled
    ok, detail, _ = streams.check_source(src, NOW)
    assert ok is False and "stopped moving" in detail
    src.media_seq = 49                                                                      # it moved: fine
    assert streams.check_source(src, NOW)[0] is True


def test_check_source_reports_errors_files_and_youtube(monkeypatch):
    hls = SimpleNamespace(kind="hls", url="https://cdn.example.com/a.m3u8", media_seq=None, seq_changed_at=None)
    monkeypatch.setattr(streams, "_fetch", _fake_fetch({"https://cdn.example.com/a.m3u8": (503, "")}))
    assert streams.check_source(hls, NOW)[0] is False
    monkeypatch.setattr(streams, "_fetch", _fake_fetch({"https://cdn.example.com/a.m3u8": (200, "<html>")}))
    assert "not an HLS playlist" in streams.check_source(hls, NOW)[1]
    monkeypatch.setattr(streams, "_fetch", _fake_fetch({"https://cdn.example.com/a.m3u8": (200, LIVE_PL.format(seq=1) + "#EXT-X-ENDLIST\n")}))
    assert streams.check_source(hls, NOW)[0] is True                                        # a finished video is healthy

    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(streams, "_fetch", boom)
    ok, detail, _ = streams.check_source(hls, NOW)
    assert ok is False and "Could not reach" in detail

    file = SimpleNamespace(kind="file", url="https://cdn.example.com/a.mp4")
    monkeypatch.setattr(streams, "_fetch", _fake_fetch({"https://cdn.example.com/a.mp4": (206, "x")}))
    assert streams.check_source(file, NOW)[0] is True
    monkeypatch.setattr(streams, "_fetch", _fake_fetch({"https://cdn.example.com/a.mp4": (404, "")}))
    assert streams.check_source(file, NOW)[0] is False

    yt = SimpleNamespace(kind="youtube", url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    for status, expect in ((200, True), (404, False), (403, False), (500, None)):
        monkeypatch.setattr(streams, "_fetch", lambda url, _s=status, **k: (_s, {}, b"{}", url))
        assert streams.check_source(yt, NOW)[0] is expect
    assert streams.check_source(SimpleNamespace(kind="facebook", url="https://x"), NOW)[0] is None


def test_checker_marks_down_after_two_fails_fails_over_and_recovers():
    from tests.test_zz_live_sell import _cleanup, _setup
    from app.db.models import AuditLog
    ev_id, fight_id, _ = _setup()
    try:
        with session_scope() as db:
            admin = _admin(db)
            a = _mk(db, admin, "Primary", "https://cdn.example.com/live/a.m3u8")
            b = _mk(db, admin, "Backup", "https://cdn.example.com/live/b.m3u8")
            streams.create_assignment(db, admin, scope_type="global", scope_id=None, mode="live", chain=[a.id, b.id])
            aid, bid = a.id, b.id
            assert streams.in_use_ids(db) == {aid, bid}
            results = {aid: (False, "The host answered 503.", None), bid: (True, "Live and moving.", 7)}
            checker = lambda src, now: results[src.id]                                    # noqa: E731
            assert streams.check_all(db, checker=checker) == []                          # first failure: still Up/unknown
            assert streams.payload(db)["name"] == "Primary"
            changes = streams.check_all(db, checker=checker)                             # second failure: Down
            assert [(c["name"], c["now"]) for c in changes] == [("Primary", "down")]
            p = streams.payload(db)
            assert (p["name"], p["chain_position"], p["source_id"]) == ("Backup", 1, bid)
            fo = streams.failovers(db)
            assert len(fo) == 1 and fo[0]["primary"] == "Primary" and fo[0]["playing"] == "Backup" and "503" in fo[0]["why"]
            assert streams.check_all(db, checker=checker) == []                          # no change, no new audit row
            results[aid] = (True, "Live and moving.", 9)
            assert [(c["name"], c["now"]) for c in streams.check_all(db, checker=checker)] == [("Primary", "up")]
            assert streams.payload(db)["name"] == "Primary" and streams.failovers(db) == []
            rows = db.scalars(select(AuditLog).where(AuditLog.action == "stream.health")).all()
            assert len(rows) >= 2 and all(r.actor_username == "system" for r in rows)     # Down and Up, each explained
            # a forced primary ignores Down
            results[aid] = (False, "x", None)
            streams.check_all(db, checker=checker); streams.check_all(db, checker=checker)
            assert streams.payload(db)["name"] == "Backup"
            streams.create_assignment(db, admin, scope_type="global", scope_id=None, mode="live", chain=[aid, bid], pin_first=True)
            assert streams.payload(db)["name"] == "Primary"
    finally:
        _cleanup(ev_id)


def test_the_stall_is_found_through_check_all_with_real_state(monkeypatch):
    with session_scope() as db:
        admin = _admin(db)
        a = _mk(db, admin, "Cam", "https://cdn.example.com/live/c.m3u8")
        streams.take(db, admin, a.id)
        monkeypatch.setattr(streams, "_fetch", _fake_fetch({"https://cdn.example.com/live/c.m3u8": (200, LIVE_PL.format(seq=10))}))
        assert streams.check_all(db) == [] and a.health == "up" and a.media_seq == 10
        a.seq_changed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=300)    # frozen for five minutes
        streams.check_all(db)
        changes = streams.check_all(db)
        assert a.health == "down" and changes and "stopped moving" in a.health_detail


def test_a_viewer_who_cannot_play_one_gets_the_next_but_never_a_dead_end():
    with session_scope() as db:
        admin = _admin(db)
        a = _mk(db, admin, "One", "https://cdn.example.com/live/1.m3u8").id
        b = _mk(db, admin, "Two", "https://cdn.example.com/live/2.m3u8").id
        streams.create_assignment(db, admin, scope_type="global", scope_id=None, mode="live", chain=[a, b])
        assert streams.payload(db)["source_id"] == a
        assert streams.payload(db, exclude=frozenset({a}))["source_id"] == b
        assert streams.payload(db, exclude=frozenset({a, b}))["source_id"] == a            # nothing left: keep retrying the first
        assert streams.payload(db, exclude=frozenset({999}))["source_id"] == a


def test_impact_text_and_undo_restore_the_replaced_setting():
    from tests.test_zz_live_sell import _cleanup, _setup
    ev_id, fight_id, _ = _setup()
    try:
        with session_scope() as db:
            admin = _admin(db)
            a = _mk(db, admin, "Main", "https://cdn.example.com/live/m.m3u8").id
            b = _mk(db, admin, "Clip", "https://cdn.example.com/c.mp4").id
            first = streams.create_assignment(db, admin, scope_type="event", scope_id=ev_id, mode="live", chain=[a], slate_message="Back soon")
            assert first.previous is None
            info = streams.impact(db, scope_type="event", scope_id=ev_id, mode="replay", chain=[b])
            assert "everyone on" in info["text"] and "Replay" in info["text"] and "replaces the current standing setting" in info["text"]
            future = streams.impact(db, scope_type="event", scope_id=ev_id, mode="live", chain=[a], starts_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1))
            assert "Nothing changes until then" in future["text"] and "replaces" not in future["text"]
            assert "message instead of video" in streams.impact(db, scope_type="global", scope_id=None, mode="off", chain=[])["text"]
            with pytest.raises(ValidationError):
                streams.impact(db, scope_type="moon", scope_id=None, mode="live", chain=[a])

            second = streams.create_assignment(db, admin, scope_type="event", scope_id=ev_id, mode="replay", chain=[b])
            assert second.previous and second.previous["mode"] == "live" and second.previous["slate_message"] == "Back soon"
            assert streams.payload(db, event_id=ev_id)["mode"] == "replay"
            msg = streams.undo_assignment(db, admin, second.id)
            assert "previous setting is back" in msg
            now = streams.list_assignments(db)
            assert len(now) == 1 and (now[0].mode, now[0].chain, now[0].slate_message) == ("live", str(a), "Back soon")
            assert streams.payload(db, event_id=ev_id)["mode"] == "auto"      # a Live HLS source with "Auto" plays as auto
            assert "removed" in streams.undo_assignment(db, admin, now[0].id)               # nothing before it: just removed
            assert streams.list_assignments(db) == []
            with pytest.raises(Exception):
                streams.undo_assignment(db, admin, 999999)
    finally:
        _cleanup(ev_id)


def test_undo_skips_sources_that_were_deleted_in_the_meantime():
    with session_scope() as db:
        admin = _admin(db)
        a = _mk(db, admin, "Old", "https://cdn.example.com/live/o.m3u8").id
        b = _mk(db, admin, "New", "https://cdn.example.com/live/n.m3u8").id
        streams.create_assignment(db, admin, scope_type="global", scope_id=None, mode="live", chain=[a])
        second = streams.create_assignment(db, admin, scope_type="global", scope_id=None, mode="live", chain=[b])
        streams.delete(db, admin, a)
        streams.undo_assignment(db, admin, second.id)
        back = streams.list_assignments(db)
        assert len(back) == 1 and back[0].chain == ""                                        # nothing playable left, no crash


def test_admin_pages_impact_undo_check_now_and_viewer_bad_param(monkeypatch):
    from tests.test_zz_live_sell import _cleanup, _setup
    ev_id, fight_id, _ = _setup()
    try:
        with session_scope() as db:
            admin = _admin(db)
            a = _mk(db, admin, "Alpha", "https://cdn.example.com/live/a.m3u8").id
            b = _mk(db, admin, "Bravo", "https://cdn.example.com/live/b.m3u8").id
        c, _ = login("admin")
        r = _post(c, "/admin/streams/assign/impact", scope="event", event_id=ev_id, mode="live", s1=a)
        assert r.json()["ok"] and "everyone on" in r.json()["text"]
        assert _post(c, "/admin/streams/assign/impact", scope="event", mode="live", s1=a).json()["ok"] is False
        assert _post(c, "/admin/streams/assign", scope="global", mode="live", s1=a, s2=b).status_code == 303

        monkeypatch.setattr(streams, "check_source", lambda src, now: (False, "The host answered 503.", None))
        _post(c, f"/admin/streams/{a}/check"); _post(c, f"/admin/streams/{a}/check")          # two failed checks
        page = c.get("/admin/streams").text
        assert "Down" in page and "The host answered 503." in page and "Failover:" in page and "Bravo" in page and "Check now" in page
        assert c.get("/_live/stream").json()["name"] == "Bravo"

        monkeypatch.setattr(streams, "check_source", lambda src, now: (True, "Live and moving.", 4))
        _post(c, f"/admin/streams/{a}/check")
        assert c.get("/_live/stream").json()["name"] == "Alpha"
        assert c.get(f"/_live/stream?bad={a}").json()["name"] == "Bravo"                       # this viewer's player asked to skip
        assert c.get(f"/_live/stream/e{ev_id}?bad=x,{a},,").status_code == 200                  # junk in ?bad= is ignored
        assert c.get(f"/_live/stream?bad={a}").headers["ETag"] != c.get("/_live/stream").headers["ETag"]

        with session_scope() as db:
            gid = streams.list_assignments(db)[0].id
        assert _post(c, f"/admin/streams/assignments/{gid}/undo").status_code == 303
        assert c.get("/_live/stream").json()["kind"] is None
    finally:
        _cleanup(ev_id)


def test_checker_command_runs_and_does_nothing_when_off(capsys):
    from app import cli
    with session_scope() as db:
        SettingsStore(db).set("stream.enabled", "false", _admin(db).id)
    cli.streams_check(SimpleNamespace(repeat=1, interval=1))
    assert "switched off" in capsys.readouterr().out
    with session_scope() as db:
        SettingsStore(db).set("stream.enabled", "true", _admin(db).id)
    cli.streams_check(SimpleNamespace(repeat=1, interval=1))
    assert "No change" in capsys.readouterr().out


def test_player_failover_code_is_present_and_valid_js():
    js = open("app/static/js/stream.js").read()
    assert "?bad=" in js and "source_id" in js and "failures === 2" in js
    node = shutil.which("node")
    if node:
        for f in ("app/static/js/stream.js", "app/static/js/streams-admin.js"):
            assert subprocess.run([node, "--check", f], capture_output=True).returncode == 0
