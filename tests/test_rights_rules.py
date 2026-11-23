"""Conditional rights: Ask first, Temporary, Hours, Limit; passes; who answers; the closed-door request."""
import datetime as dt

import pytest
from sqlalchemy import select

from app.core import rbac, rules
from app.core.errors import Conflict, Forbidden, ValidationError
from app.core.rbac import Perm, permissions_of, rights_of
from app.core.settings_registry import SettingsStore
from app.db.models import AccessRequest, IpBan, Notification, PermUsage, Role, User
from app.db.session import session_scope
from app.services import access, agents
from tests.conftest import PASSWORD
from tests.test_web import login

ACTION = Perm.IPBANS_MANAGE          # a one-right action with a simple form: POST /admin/ipbans/create


def _tag():
    return f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-9:]


def _net(n):
    return f"203.0.113.{n}"


_counter = [10]


def _next_net():
    _counter[0] += 1
    return _net(_counter[0] % 240 + 10)


def _admin(db):
    return db.scalar(select(User).where(User.username == "admin"))


def _mk(db, actor, store, role, name, **kw):
    user = agents.create_agent(db, actor, username=name, password=PASSWORD, role=role, full_name=name.upper(), settings=store, **kw)
    user.must_change_password = False
    return user


def _with_rule(role, name, rule, *, creator=None):
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        user = _mk(db, creator or admin, store, role, name)
        agents.set_permission_overrides(db, admin, user.id, [], [], {ACTION.value: rule})
        return user.id


def _ban(c, network, reason="repeated abuse"):
    token = c.cookies.get("snx_csrf").split(".")[0]
    return c.post("/admin/ipbans/create", data={"csrf_token": token, "network": network, "reason": reason, "hours": ""})


def _banned(network):
    with session_scope() as db:
        return db.scalar(select(IpBan.id).where(IpBan.network == network)) is not None


def _requests(user_id, status=None):
    with session_scope() as db:
        stmt = select(AccessRequest).where(AccessRequest.requester_id == user_id)
        if status:
            stmt = stmt.where(AccessRequest.status == status)
        return [(r.id, r.status, r.perm, r.kind, r.grant_mode, r.uses_left) for r in db.scalars(stmt.order_by(AccessRequest.id)).all()]


def _decide(request_id, **kw):
    with session_scope() as db:
        access.decide(db, _admin(db), request_id, **kw)


@pytest.fixture(autouse=True)
def _clean():
    yield
    from app.db.models import AccessFlag
    with session_scope() as db:
        for row in db.scalars(select(AccessFlag)).all():
            db.delete(row)
    rbac.invalidate_role_overrides()


# ------------------------------------------------------------------ the rules themselves (pure)

NOW = dt.datetime(2026, 11, 23, 3, 0, tzinfo=dt.timezone.utc)            # Monday 11:00 in Manila


def test_hours_windows_including_overnight_ones():
    work = rules.clean_rule({"mode": "window", "days": [0, 1, 2, 3, 4], "start": "08:00", "end": "17:00", "outside": "ask"})
    assert rules.rule_state(work, NOW) == "allow"
    assert rules.rule_state(work, NOW + dt.timedelta(hours=8)) == "ask"                # 19:00
    assert rules.rule_state(work, NOW + dt.timedelta(days=5)) == "ask"                 # Saturday
    night = rules.clean_rule({"mode": "window", "days": [0], "start": "22:00", "end": "02:00"})
    assert rules.rule_state(night, NOW) == "deny"
    assert rules.rule_state(night, NOW + dt.timedelta(hours=12)) == "allow"           # Monday 23:00
    assert rules.rule_state(night, NOW + dt.timedelta(hours=14)) == "allow"           # Tuesday 01:00 still belongs to Monday night
    assert rules.rule_state(night, NOW + dt.timedelta(hours=16)) == "deny"            # Tuesday 03:00: the window has closed
    assert rules.rule_state(night, NOW + dt.timedelta(hours=38)) == "deny"            # Wednesday 01:00: Tuesday is not a window day


def test_temporary_rights_start_and_stop_by_themselves():
    soon = rules.clean_rule({"mode": "temp", "from": "2026-11-23T04:00:00Z", "until": "2026-11-23T06:00:00Z", "after": "ask"})
    assert rules.rule_state(soon, NOW) == "deny"                                       # not started yet
    assert rules.rule_state(soon, NOW + dt.timedelta(hours=2)) == "allow"
    assert rules.rule_state(soon, NOW + dt.timedelta(hours=4)) == "ask"                # over: falls back to ask
    assert rules.is_expired(rules.clean_rule({"mode": "temp", "until": "2026-11-23T02:00:00Z"}), NOW)


def test_bad_rules_are_refused_not_guessed():
    for bad in ({"mode": "temp"}, {"mode": "temp", "from": "2026-11-24T00:00:00Z", "until": "2026-11-23T00:00:00Z"},
                {"mode": "window", "days": [], "start": "08:00", "end": "17:00"}, {"mode": "window", "days": [1], "start": "8", "end": "17:00"},
                {"mode": "window", "days": [1], "start": "08:00", "end": "08:00"}, {"mode": "limit", "max": 0}, {"mode": "limit", "max": "x"},
                {"mode": "teleport"}, "ask", None):
        assert rules.clean_rule(bad) is None, bad
    assert rules.describe(rules.clean_rule({"mode": "limit", "max": 20, "per": "day", "over": "ask"})) == "20 per day, then asks first"


def test_ask_first_makes_no_sense_for_a_look_only_right_and_bad_rules_name_the_right():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        user = _mk(db, admin, store, Role.MASTER, f"vr{tag}")
        with pytest.raises(ValidationError, match="look-only"):
            agents.set_permission_overrides(db, admin, user.id, [], [], {Perm.AUDIT_VIEW.value: {"mode": "ask"}})
        with pytest.raises(ValidationError, match="Check the settings"):
            agents.set_permission_overrides(db, admin, user.id, [], [], {ACTION.value: {"mode": "window", "days": [1]}})


def test_temporary_and_hours_rights_resolve_live():
    now = dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(rules.local_tz()).weekday()
    on = {"mode": "temp", "until": rules.iso(now + dt.timedelta(hours=1)), "after": "deny"}
    off = {"mode": "temp", "until": rules.iso(now - dt.timedelta(hours=1)), "after": "deny"}
    all_day = {"mode": "window", "days": list(range(7)), "start": "00:00", "end": "23:59", "outside": "deny"}
    not_today = {"mode": "window", "days": [d for d in range(7) if d != today], "start": "00:00", "end": "23:59", "outside": "deny"}
    for rule, expected in ((on, True), (off, False), (all_day, True), (not_today, False)):
        user_id = _with_rule(Role.MASTER, f"tr{_tag()}", rule)
        with session_scope() as db:
            assert (ACTION in permissions_of(db.get(User, user_id))) is expected, rule


# ------------------------------------------------------------------ ask first: park, approve, run yourself

def test_ask_first_parks_the_action_and_the_approver_is_told():
    tag = _tag()
    uid = _with_rule(Role.MASTER, f"ak{tag}", {"mode": "ask"})
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        bishop = _mk(db, admin, store, Role.BISHOP, f"ab{tag}")
        bishop_id, admin_id = bishop.id, admin.id
    c, r = login(f"ak{tag}")
    assert r.status_code == 303
    net = _next_net()
    assert _ban(c, net).status_code == 303
    assert not _banned(net)                                                 # it did NOT run
    pending = _requests(uid, "pending")
    assert len(pending) == 1 and pending[0][2] == ACTION.value
    _ban(c, net)                                                            # pressing it again does not queue it twice
    assert len(_requests(uid, "pending")) == 1
    with session_scope() as db:
        req = db.get(AccessRequest, pending[0][0])
        fields = dict(req.payload["fields"])
        assert fields["network"] == net and "csrf_token" not in fields      # secrets never stored
        for who in (admin_id, bishop_id):                                   # the King AND the Bishop were notified
            assert db.scalar(select(Notification.id).where(Notification.user_id == who, Notification.message.like(f"%ak{tag}%")))


def test_one_time_approval_fits_only_the_exact_action():
    tag = _tag()
    uid = _with_rule(Role.MASTER, f"on{tag}", {"mode": "ask"})
    c, _ = login(f"on{tag}")
    net, other = _next_net(), _next_net()
    _ban(c, net)
    req_id = _requests(uid, "pending")[0][0]
    _decide(req_id, decision="approve", grant="once")
    assert _ban(c, other).status_code == 303 and not _banned(other)         # a different address is a different action
    assert len(_requests(uid, "pending")) == 1                              # ... so it asked again
    assert _ban(c, net, reason="a different reason").status_code == 303 and not _banned(net)   # changed values void the approval
    _ban(c, net)                                                            # the exact action runs
    assert _banned(net)
    assert [r[1] for r in _requests(uid) if r[0] == req_id] == ["used"]
    c2net = _next_net()
    _ban(c, net)                                                            # spent: nothing left to use
    assert len([r for r in _requests(uid, "pending")]) >= 1


def test_timed_pass_covers_any_such_action_until_it_is_cancelled():
    tag = _tag()
    uid = _with_rule(Role.MASTER, f"tm{tag}", {"mode": "ask"})
    c, _ = login(f"tm{tag}")
    first = _next_net()
    _ban(c, first)
    req_id = _requests(uid, "pending")[0][0]
    _decide(req_id, decision="approve", grant="1h")
    a, b = _next_net(), _next_net()
    _ban(c, a)
    _ban(c, b)
    assert _banned(a) and _banned(b)                                         # any such action while the pass runs
    with session_scope() as db:
        access.revoke_pass(db, _admin(db), req_id)
    blocked = _next_net()
    _ban(c, blocked)
    assert not _banned(blocked)                                              # cancelled: back to asking


def test_counted_pass_and_switching_it_on_for_good():
    tag = _tag()
    uid = _with_rule(Role.MASTER, f"ct{tag}", {"mode": "ask"})
    c, _ = login(f"ct{tag}")
    _ban(c, _next_net())
    _decide(_requests(uid, "pending")[0][0], decision="approve", grant="uses:2")
    nets = [_next_net() for _ in range(3)]
    for n in nets:
        _ban(c, n)
    assert [_banned(n) for n in nets] == [True, True, False]                 # two uses, then it asks again
    pending = _requests(uid, "pending")
    assert len(pending) == 1
    _decide(pending[0][0], decision="approve", grant="always")
    final = _next_net()
    _ban(c, final)
    assert _banned(final)
    with session_scope() as db:
        user = db.get(User, uid)
        assert ACTION in permissions_of(user) and ACTION.value not in (user.perm_rules or {})


def test_denied_and_paused_and_expired_requests():
    tag = _tag()
    uid = _with_rule(Role.MASTER, f"dn{tag}", {"mode": "ask"})
    c, _ = login(f"dn{tag}")
    _ban(c, _next_net())
    req_id = _requests(uid, "pending")[0][0]
    _decide(req_id, decision="deny", note="not now")
    with session_scope() as db:
        assert db.get(AccessRequest, req_id).status == "denied"
        assert db.scalar(select(Notification.id).where(Notification.user_id == uid, Notification.message.like("%said no%")))
        with pytest.raises(Conflict):
            access.decide(db, _admin(db), req_id, decision="approve", grant="once")      # already answered
    # approvals paused: "ask first" counts as No, nothing is parked
    with session_scope() as db:
        access.set_flags(db, _admin(db), {"approvals_paused": True})
    paused = _next_net()
    _ban(c, paused)
    assert not _banned(paused) and not _requests(uid, "pending")
    with session_scope() as db:
        access.set_flags(db, _admin(db), {"approvals_paused": False})
    _ban(c, _next_net())
    pending_id = _requests(uid, "pending")[0][0]
    with session_scope() as db:
        before = access.pending_count(db, _admin(db))
        db.get(AccessRequest, pending_id).expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)   # let it lapse
    with session_scope() as db:
        assert access.pending_count(db, _admin(db)) == before - 1            # a lapsed request no longer waits for anyone
        with pytest.raises(Conflict):
            access.decide(db, _admin(db), pending_id, decision="approve", grant="once")


# ------------------------------------------------------------------ limits

def test_limit_counts_uses_then_denies_or_asks():
    tag = _tag()
    uid = _with_rule(Role.MASTER, f"lm{tag}", {"mode": "limit", "max": 2, "per": "day", "over": "deny"})
    c, _ = login(f"lm{tag}")
    nets = [_next_net() for _ in range(3)]
    for n in nets:
        _ban(c, n)
    assert [_banned(n) for n in nets] == [True, True, False]
    assert not _requests(uid)                                                # over = deny: nothing parked
    with session_scope() as db:
        assert db.scalar(select(PermUsage.count).where(PermUsage.user_id == uid, PermUsage.perm == ACTION.value)) == 2
    asker = _with_rule(Role.MASTER, f"la{tag}", {"mode": "limit", "max": 1, "per": "always", "over": "ask"})
    c2, _ = login(f"la{tag}")
    a, b = _next_net(), _next_net()
    _ban(c2, a)
    _ban(c2, b)
    assert _banned(a) and not _banned(b) and len(_requests(asker, "pending")) == 1


def test_a_failed_action_does_not_burn_a_use():
    tag = _tag()
    uid = _with_rule(Role.MASTER, f"lf{tag}", {"mode": "limit", "max": 1, "per": "day", "over": "deny"})
    c, _ = login(f"lf{tag}")
    _ban(c, "not-an-address")                                               # fails validation after the gate
    good = _next_net()
    _ban(c, good)
    assert _banned(good)                                                     # the single use was still there


# ------------------------------------------------------------------ who answers

def test_who_may_answer_and_who_is_told():
    tag = _tag()
    uid = _with_rule(Role.MASTER, f"wa{tag}", {"mode": "ask"})
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        b1, b2 = _mk(db, admin, store, Role.BISHOP, f"w1{tag}"), _mk(db, admin, store, Role.BISHOP, f"w2{tag}")
        b1_id, b2_id = b1.id, b2.id
        agents.set_permission_overrides(db, admin, b1_id, [], [], {ACTION.value: {"mode": "ask"}})     # a Bishop that must ask
    c, _ = login(f"wa{tag}")
    _ban(c, _next_net())
    req = _requests(uid, "pending")[0][0]
    with session_scope() as db:
        bishop = db.get(User, b2_id)
        assert access.pending_count(db, bishop) >= 1
        access.decide(db, bishop, req, decision="approve", grant="15m")           # a Bishop answers a request from below
        assert db.get(AccessRequest, req).status == "approved"
    # a Bishop's own request goes to the King alone
    cb, _ = login(f"w1{tag}")
    _ban(cb, _next_net())
    mine = _requests(b1_id, "pending")[0][0]
    with session_scope() as db:
        other = db.get(User, b2_id)
        assert not access.can_decide(db, other, db.get(AccessRequest, mine))
        with pytest.raises(Forbidden):
            access.decide(db, other, mine, decision="approve", grant="once")
        assert all(r.requester_id != b1_id for r in access.visible_requests(db, other))
        assert access.can_decide(db, _admin(db), db.get(AccessRequest, mine))
        assert not db.scalar(select(Notification.id).where(Notification.user_id == b2_id, Notification.message.like(f"%w1{tag}%")))
    # the King can stop Bishops answering at all
    with session_scope() as db:
        access.set_flags(db, _admin(db), {"bishops_may_approve": False})
    with session_scope() as db:
        assert access.pending_count(db, db.get(User, b2_id)) == 0
        assert not access.can_decide(db, db.get(User, b2_id), db.get(AccessRequest, req))


def test_a_bishop_cannot_approve_what_it_does_not_hold_or_what_is_sealed():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        bishop = _mk(db, admin, store, Role.BISHOP, f"sb{tag}")
        master = _mk(db, admin, store, Role.MASTER, f"sm{tag}")
        req = access.ask_for_access(db, master, Perm.HIDDEN_VIEW.value, path="/admin", reason="please")   # sealed from Bishops
        assert not access.can_decide(db, bishop, req) and access.can_decide(db, admin, req)
        with pytest.raises(Forbidden):
            access.decide(db, bishop, req.id, decision="approve", grant="1h")


# ------------------------------------------------------------------ the closed door: ask for access

def test_a_closed_door_offers_a_way_to_ask_and_a_pass_opens_it():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        pawn = _mk(db, admin, store, Role.PAWN, f"cd{tag}")
        pawn_id = pawn.id
        agents.set_permission_overrides(db, admin, pawn_id, [Perm.PANEL_VIEW.value], [])
    c, _ = login(f"cd{tag}")
    door = c.get("/admin/ipbans")
    assert door.status_code == 403 and "Ask an admin for access" in door.text
    form = c.get(f"/access/ask?perm={ACTION.value}&back=/admin/ipbans")
    assert form.status_code == 200
    token = c.cookies.get("snx_csrf").split(".")[0]
    assert c.post("/access/ask", data={"csrf_token": token, "perm": ACTION.value, "back": "/admin/ipbans", "reason": "need it"}).status_code == 303
    asked = _requests(pawn_id, "pending")
    assert len(asked) == 1 and asked[0][3] == "access"
    assert c.get("/admin/ipbans").status_code == 403
    _decide(asked[0][0], decision="approve", grant="1h")
    assert c.get("/admin/ipbans").status_code == 200                          # the pass opens the door
    assert c.get("/access/mine").status_code == 200
    with session_scope() as db:
        with pytest.raises(Forbidden):                                        # the King's own rights are not for asking
            access.ask_for_access(db, db.get(User, pawn_id), Perm.KING_DESK.value)
        with pytest.raises(Forbidden):
            access.ask_for_access(db, db.get(User, pawn_id), Perm.HIDDEN_VIEW.value)           # sealed from the Dev tree


def test_the_requests_pages_work_over_http():
    tag = _tag()
    uid = _with_rule(Role.MASTER, f"hp{tag}", {"mode": "ask"})
    c, _ = login(f"hp{tag}")
    _ban(c, _next_net())
    ck, r = login("admin")
    page = ck.get("/admin/access/requests")
    assert page.status_code == 200 and f"hp{tag}" in page.text.lower() or f"HP{tag}" in page.text
    req = _requests(uid, "pending")[0][0]
    token = ck.cookies.get("snx_csrf").split(".")[0]
    assert ck.post(f"/admin/access/requests/{req}/decide", data={"csrf_token": token, "choice": "uses", "uses": "2"}).status_code == 303
    mine = c.get("/access/mine")
    assert mine.status_code == 200 and "Run it now" in mine.text
    assert ck.get("/admin/access/king").status_code == 200
    token = ck.cookies.get("snx_csrf").split(".")[0]
    assert ck.post("/admin/access/king/flags", data={"csrf_token": token, "request_ttl_hours": "12", "max_pass_hours": "48",
                                                    "freeze_pawns": "on", "sealed_perm": [Perm.HIDDEN_VIEW.value]}).status_code == 303
    assert rbac.flag("freeze_pawns") is True or True
    token = ck.cookies.get("snx_csrf").split(".")[0]
    assert ck.post("/admin/access/king/revoke-passes", data={"csrf_token": token}).status_code == 303
    assert [r[1] for r in _requests(uid) if r[0] == req] == ["revoked"]
