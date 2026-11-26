"""Credit rule for watching a stream: who is gated, when the picture is withheld, the grace until the fight ends."""
import datetime as dt

import pytest

from app.core import ttlcache
from app.db.models import EventStatus, FightStatus, Side, TicketStatus
from app.services import betting, events, stream_access as sa
from app.services import streams as streams_svc
from tests.conftest import fund_user
from tests.test_network import chain  # noqa: F401  (fixture)
from tests.test_web import login

PLAYING = {"status": "playing", "kind": "hls", "src": "https://cdn.example/live.m3u8", "embed": None, "name": "Cam 1",
           "source_id": 1, "scope": "default", "mode": "live", "dvr_window": 120, "autoplay_muted": True}


def _rule(w, on: bool):
    w.store.set("stream.require_credit", "true" if on else "false", w.admin.id)


@pytest.fixture()
def rule_on(chain):
    _rule(chain, True)
    yield chain
    _rule(chain, False)


# ---------------------------------------------------------------- the pure rule (no database)
@pytest.mark.parametrize("balance,pending,grace,expected", [
    (5000, False, True, "open"),
    (2000, False, True, "open"),          # exactly the minimum is enough
    (1999, False, True, "locked"),
    (0, False, True, "locked"),
    (1000, True, True, "grace"),          # just bet, credit now low: finish the fight
    (0, True, True, "grace"),
    (0, True, False, "locked"),           # grace switched off: the card appears at once
    (9000, True, True, "open"),           # plenty of credit: a pending fight changes nothing
])
def test_decide_table(balance, pending, grace, expected):
    assert sa.decide(balance_cents=balance, minimum_cents=2000, has_pending_fight=pending, grace_on=grace).state == expected


def test_settings_default_to_off_and_to_the_minimum_bet(chain):
    cfg = sa.settings_of(chain.store)
    assert cfg["on"] is False and cfg["minimum"] == int(chain.store.get("payout.min_stake_cents")) == 2000
    assert cfg["grace"] is True and cfg["pause"] == 8


# ---------------------------------------------------------------- who is gated
def test_only_the_users_branch_is_gated(chain):
    w = chain
    assert sa.is_gated(w.master) and sa.is_gated(w.cavo) and sa.is_gated(w.kub)
    assert not sa.is_gated(w.admin) and not sa.is_gated(None)


# ---------------------------------------------------------------- the payload
def test_rule_off_changes_nothing(chain):
    out = sa.apply(chain.db, chain.kub, chain.store, PLAYING)
    assert out is PLAYING


def test_no_credit_gets_no_address_at_all(rule_on):
    w = rule_on
    out = sa.apply(w.db, w.kub, w.store, PLAYING)
    assert out["status"] == "locked" and out["gate"]["state"] == "locked"
    assert out["src"] is None and out["embed"] is None and out["source_id"] is None and out["name"] is None
    assert "live.m3u8" not in repr(out)                          # nothing to copy
    assert PLAYING["src"] == "https://cdn.example/live.m3u8"     # the shared cached payload is never modified
    assert out["gate"]["need_cents"] == 2000 and out["gate"]["delay_seconds"] == 8


def test_staff_and_slates_are_never_gated(rule_on):
    w = rule_on
    assert sa.apply(w.db, w.admin, w.store, PLAYING) is PLAYING
    for status in ("slate", "off", "sample"):
        assert sa.apply(w.db, w.kub, w.store, {"status": status}) == {"status": status}


def test_credit_at_the_minimum_watches_and_just_under_does_not(rule_on):
    w = rule_on
    fund_user(w.db, w.admin, w.kub, 2000)
    assert sa.apply(w.db, w.kub, w.store, PLAYING)["status"] == "playing"
    fund_user(w.db, w.admin, w.cavo, 1999)
    assert sa.apply(w.db, w.cavo, w.store, PLAYING)["status"] == "locked"


def test_a_bet_that_empties_the_wallet_keeps_the_viewer_watching_until_the_fight_ends(rule_on):
    w = rule_on
    db, admin, store, kub = w.db, w.admin, w.store, w.kub
    fund_user(db, admin, kub, 3000)
    event = events.create_event(db, admin, title=f"Gate {w.tag}", event_date=dt.date.today(), settings=store, fight_count=2)
    events.set_event_status(db, admin, event.id, EventStatus.OPEN)
    fight = events.fights_of(db, event.id)[0]
    ticket = betting.place_bet(db, kub, fight_id=fight.id, side=Side.MERON, stake_cents=2500, settings=store,
                               idempotency_key=f"gate-{w.tag}")
    assert sa.balance_of(db, kub.id) == 500 and sa.has_pending_fight(db, kub.id, fight_id=fight.id)
    out = sa.apply(db, kub, store, PLAYING, fight_id=fight.id, event_id=event.id)                                   # credit is low, but the fight is not over
    assert out["status"] == "playing" and out["src"] and out["gate"]["state"] == "grace"
    fight.status, ticket.status = FightStatus.SETTLED, TicketStatus.SETTLED   # the fight is declared, the bet lost
    db.flush()
    assert not sa.has_pending_fight(db, kub.id, fight_id=fight.id)
    assert sa.apply(db, kub, store, PLAYING, fight_id=fight.id)["status"] == "locked"            # now the top-up card
    fund_user(db, admin, kub, 5000)                                           # load approved: resumes by itself
    assert sa.apply(db, kub, store, PLAYING, fight_id=fight.id)["status"] == "playing"


def test_an_old_undeclared_fight_does_not_keep_a_viewer_watching_other_fights(rule_on):
    w = rule_on
    db, admin, store, kub = w.db, w.admin, w.store, w.kub
    fund_user(db, admin, kub, 3000)
    event = events.create_event(db, admin, title=f"Stale {w.tag}", event_date=dt.date.today(), settings=store, fight_count=3)
    events.set_event_status(db, admin, event.id, EventStatus.OPEN)
    first, second = events.fights_of(db, event.id)[:2]
    betting.place_bet(db, kub, fight_id=first.id, side=Side.MERON, stake_cents=2500, settings=store,
                      idempotency_key=f"stale-{w.tag}")                       # bet on fight 1, which never gets declared
    assert sa.apply(db, kub, store, PLAYING, fight_id=first.id)["status"] == "playing"     # watching the fight they bet on: grace
    assert sa.apply(db, kub, store, PLAYING, fight_id=second.id)["status"] == "locked"     # watching another fight: no grace


def test_a_win_that_lifts_the_balance_never_shows_the_prompt(rule_on):
    w = rule_on
    fund_user(w.db, w.admin, w.kub, 1000)
    assert sa.apply(w.db, w.kub, w.store, PLAYING)["status"] == "locked"
    fund_user(w.db, w.admin, w.kub, 9000)                                      # payout lands
    assert sa.apply(w.db, w.kub, w.store, PLAYING)["status"] == "playing"


def test_a_waiting_load_request_is_reported(rule_on, monkeypatch):
    w = rule_on
    monkeypatch.setattr(sa, "has_pending_load", lambda db, uid: True)
    assert sa.apply(w.db, w.kub, w.store, PLAYING)["gate"]["pending_load"] is True


# ---------------------------------------------------------------- the live endpoints use it
def test_the_live_endpoints_withhold_the_address_from_a_viewer_with_no_credit(rule_on, monkeypatch):
    w = rule_on
    monkeypatch.setattr(streams_svc, "payload", lambda *a, **k: dict(PLAYING))
    w.kub.must_change_password = False
    w.db.flush()
    w.db.commit()
    ttlcache.clear("live")
    c, _ = login(w.kub.username)
    body = c.get("/_live/stream").json()
    assert body["status"] == "locked" and "live.m3u8" not in str(body) and body["gate"]["state"] == "locked"
    admin_client, _ = login("admin")
    ttlcache.clear("live")
    assert admin_client.get("/_live/stream").json()["src"] == "https://cdn.example/live.m3u8"
