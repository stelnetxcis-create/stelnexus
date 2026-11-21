"""Open launch of Bola, the dev-only Seller Desk, IP bans, and account edit/remove."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import rbac
from app.core.config import get_settings
from app.core.errors import Conflict, Forbidden, NotFound, ValidationError
from app.core.rbac import Perm, permissions_for, permissions_of
from app.core.settings_registry import SettingsStore
from app.db.models import AuditLog, IpBan, Role, Status, User
from app.db.session import session_scope
from app.services import account_admin, agents, ipbans, players
from tests.conftest import PASSWORD, load_credit
from tests.test_web import app, login


def _tag():
    return f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-9:]


def _admin(db):
    return db.scalar(select(User).where(User.username == "admin"))


def _mk(db, admin, store, role, name, parent=None, grants=None):
    user = agents.create_agent(db, parent or admin, username=name, password=PASSWORD, role=role,
                               full_name=name.upper(), settings=store)
    user.must_change_password = False
    if grants is not None:
        agents.set_pawn_grants(db, admin, user.id, grants)
    return user


# ------------------------------------------------------------------ Bola opened, Seller Desk dev-only

def test_bola_is_open_to_every_role_by_default():
    assert get_settings().BOLA_DEV_ONLY is False and not rbac.bola_dev_only()
    for role in (Role.MASTER, Role.AGENT, Role.SUB_AGENT):
        assert Perm.BOLA_VIEW in permissions_for(role), role
    for role in (Role.AGENT, Role.SUB_AGENT):
        assert Perm.BOLA_PLACE in permissions_for(role), role


def test_bola_tile_and_nav_reach_a_kubrador_with_no_coming_soon():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        store.set("bola.enabled", "true", admin.id)
        master = _mk(db, admin, store, Role.MASTER, f"om{tag}")
        cavo = _mk(db, admin, store, Role.AGENT, f"oc{tag}", parent=master)
        _mk(db, admin, store, Role.SUB_AGENT, f"ok{tag}", parent=cavo)
    c, r = login(f"ok{tag}")
    assert r.status_code == 303
    html = c.get("/dashboard").text
    assert 'href="/bola"' in html
    assert c.get("/bola").status_code == 200
    number = html.split('data-game="number"')[0].rsplit("<", 1)[-1]
    assert "game-tile is-live" in number or "is-live" in html


def test_seller_desk_is_gone():
    """The experimental Seller Desk (/sell) was removed: no page, no nav link, no sell.desk permission."""
    assert not any(p.value == "sell.desk" for p in Perm)
    c, _ = login("admin")
    assert c.get("/sell", headers={"Accept": "application/json"}).status_code == 404
    assert 'href="/sell"' not in c.get("/dashboard").text


# ------------------------------------------------------------------ IP bans

def test_ban_parsing_and_limits():
    assert str(ipbans.parse_network("203.0.113.9")) == "203.0.113.9/32"
    assert str(ipbans.parse_network("203.0.113.77/24")) == "203.0.113.0/24"
    for bad in ("", "nope", "10.0.0.0/8", "0.0.0.0/0", "2001:db8::/32", "1.2.3.4.5"):
        with pytest.raises(ValidationError):
            ipbans.parse_network(bad)


def test_cannot_ban_yourself_or_the_admins_address():
    with session_scope() as db:
        admin = _admin(db)
        admin.last_login_ip = "198.51.100.7"
        db.flush()
        with pytest.raises(Forbidden):
            ipbans.ban(db, admin, network="198.51.100.0/24", reason="oops", actor_ip="203.0.113.1")
        with pytest.raises(Forbidden):
            ipbans.ban(db, admin, network="203.0.113.1", reason="oops", actor_ip="203.0.113.1")
        with pytest.raises(ValidationError):
            ipbans.ban(db, admin, network="203.0.113.50", reason="x", actor_ip="203.0.113.1")   # reason too short
        admin.last_login_ip = None
        db.flush()


def test_a_banned_address_is_blocked_everywhere_and_unban_restores_it():
    ip = "203.0.113.9"
    with session_scope() as db:
        row = ipbans.ban(db, _admin(db), network=ip, reason="spam", actor_ip="127.0.0.1")
        ban_id = row.id
    try:
        blocked = TestClient(app, client=(ip, 50000), follow_redirects=False)
        assert blocked.get("/login").status_code == 403
        assert blocked.get("/static/css/base.css").status_code == 403
        assert blocked.get("/healthz").status_code == 200                       # never block health checks
        other = TestClient(app, client=("203.0.113.10", 50000), follow_redirects=False)
        assert other.get("/login").status_code == 200
    finally:
        with session_scope() as db:
            ipbans.unban(db, _admin(db), ban_id)
    assert TestClient(app, client=(ip, 50000)).get("/login").status_code == 200
    with session_scope() as db:
        actions = {a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(10))}
    assert {"ip.ban", "ip.unban"} <= actions


def test_expired_bans_do_not_block():
    with session_scope() as db:
        db.add(IpBan(network="203.0.113.77", reason="old", expires_at=dt.datetime.now(dt.timezone.utc)
                     - dt.timedelta(hours=1)))
    ipbans.invalidate()
    assert TestClient(app, client=("203.0.113.77", 1)).get("/login").status_code == 200
    with session_scope() as db:
        for row in db.scalars(select(IpBan).where(IpBan.network == "203.0.113.77")):
            db.delete(row)
    ipbans.invalidate()


def test_ip_ban_page_is_admin_or_granted_pawn_only():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        master = _mk(db, admin, store, Role.MASTER, f"im{tag}")
        _mk(db, admin, store, Role.PAWN, f"ip{tag}", grants=[])
        _mk(db, admin, store, Role.PAWN, f"ig{tag}", grants=["ipbans:manage"])
    for name, code in ((f"im{tag}", 403), (f"ip{tag}", 403), (f"ig{tag}", 200), ("admin", 200)):
        c, r = login(name)
        assert r.status_code == 303, name
        assert c.get("/admin/ipbans", headers={"Accept": "application/json"}).status_code == code, name


# ------------------------------------------------------------------ edit and remove accounts

def test_edit_user_details_audited_and_username_unique():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        a = _mk(db, admin, store, Role.MASTER, f"ea{tag}")
        b = _mk(db, admin, store, Role.MASTER, f"eb{tag}")
        account_admin.edit_user(db, admin, a.id, username=f"ea{tag}x", full_name="New Name", contact="0917")
        assert (a.username, a.full_name, a.contact) == (f"ea{tag}x", "New Name", "0917")
        with pytest.raises(Conflict):
            account_admin.edit_user(db, admin, a.id, username=b.username)
        with pytest.raises(ValidationError):
            account_admin.edit_user(db, admin, a.id, username="a b")
        with pytest.raises(ValidationError):
            account_admin.edit_user(db, admin, a.id, full_name="  ")
        row = db.scalar(select(AuditLog).where(AuditLog.action == "account.edit").order_by(AuditLog.id.desc()))
        assert row.entity_id == str(a.id)


def test_nobody_can_edit_or_remove_the_admin_and_pawns_cannot_touch_pawns():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        victim = _mk(db, admin, store, Role.MASTER, f"pv{tag}")
        boss = _mk(db, admin, store, Role.PAWN, f"pb{tag}", grants=["accounts:edit", "accounts:remove"])
        other = _mk(db, admin, store, Role.PAWN, f"po{tag}", grants=[])
        with pytest.raises(NotFound):
            account_admin.edit_user(db, boss, admin.id, full_name="Hacked")
        with pytest.raises(NotFound):
            account_admin.remove_user(db, boss, admin.id, reason="take over")
        with pytest.raises(NotFound):
            account_admin.remove_user(db, admin, admin.id, reason="no way")
        with pytest.raises(Forbidden):
            account_admin.edit_user(db, boss, other.id, full_name="X")
        with pytest.raises(Forbidden):
            account_admin.remove_user(db, boss, other.id, reason="not yours")
        with pytest.raises((Forbidden, ValidationError)):
            account_admin.remove_user(db, boss, boss.id, reason="myself")
        account_admin.edit_user(db, boss, victim.id, full_name="Edited By Pawn")      # allowed by the grant
        # without the grant the same call is refused
        with pytest.raises(Forbidden):
            account_admin.edit_user(db, other, victim.id, full_name="nope")
        with pytest.raises(Forbidden):
            account_admin.remove_user(db, other, victim.id, reason="no grant")


def test_remove_user_closes_signs_out_and_is_refused_while_money_or_downline_remain():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        master = _mk(db, admin, store, Role.MASTER, f"rm{tag}")
        cavo = _mk(db, admin, store, Role.AGENT, f"rc{tag}", parent=master)
        # a Master with an active Cavo cannot go
        with pytest.raises(Conflict):
            account_admin.remove_user(db, admin, master.id, reason="cleanup test")
        # a Cavo that still holds credit cannot go
        from app.services import accounts, ledger
        wallet = accounts.wallet_for_user(db, cavo)
        ledger.post(db, kind="test.fund", entries=[(wallet, -500), (accounts.house(db), +500)],
                    actor=admin, memo="test credit")
        with pytest.raises(Conflict) as exc:
            account_admin.remove_user(db, admin, cavo.id, reason="cleanup test")
        assert "credit" in str(exc.value) and "500" not in str(exc.value)    # no amounts leak to a Pawn
        ledger.post(db, kind="test.drain", entries=[(wallet, +500), (accounts.house(db), -500)],
                    actor=admin, memo="drain")
        with pytest.raises(ValidationError):
            account_admin.remove_user(db, admin, cavo.id, reason="no")         # reason too short
        account_admin.remove_user(db, admin, cavo.id, reason="left the business")
        assert cavo.status == Status.CLOSED
        with pytest.raises(Conflict):
            account_admin.remove_user(db, admin, cavo.id, reason="twice")
        with pytest.raises(Conflict):
            account_admin.edit_user(db, admin, cavo.id, full_name="Zombie")
        account_admin.remove_user(db, admin, master.id, reason="left the business")   # downline gone now
    c, r = login(f"rc{tag}")
    assert c.get("/dashboard").status_code == 303                              # a closed account has no session


def test_players_edit_and_remove_rules():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        p = players.register_online_player(db, admin, username=f"rp{tag}", password=PASSWORD,
                                           full_name="Pat", birth_date=dt.date(1990, 1, 1),
                                           gcash_number="09171234567", settings=store)
        p.kyc_verified = True
        account_admin.edit_player(db, admin, p.id, full_name="Patricia", gcash_number="09180000000",
                                  notes="vip")
        assert p.full_name == "Patricia" and players.decrypt_field(p.gcash_enc) == "09180000000"
        with pytest.raises(ValidationError):
            account_admin.edit_player(db, admin, p.id, gcash_number="123")
        other = players.register_online_player(db, admin, username=f"rq{tag}", password=PASSWORD,
                                               full_name="Q", birth_date=dt.date(1990, 1, 1),
                                               gcash_number="09171234567", settings=store)
        with pytest.raises(Conflict):
            account_admin.edit_player(db, admin, p.id, username=other.username)
        load_credit(db, admin, store, p, 10_000)
        with pytest.raises(Conflict):
            account_admin.remove_player(db, admin, p.id, reason="cleanup test")      # holds credit
        account_admin.remove_player(db, admin, other.id, reason="cleanup test")      # empty: fine
        assert other.status == Status.CLOSED


def test_accounts_page_and_forms_work_for_a_granted_pawn_over_http():
    tag = _tag()
    with session_scope() as db:
        admin, store = _admin(db), SettingsStore(db)
        target = _mk(db, admin, store, Role.MASTER, f"ht{tag}")
        target_id = target.id
        _mk(db, admin, store, Role.PAWN, f"hp{tag}", grants=["accounts:edit"])
    c, r = login(f"hp{tag}")
    assert r.status_code == 303
    page = c.get("/admin/accounts")
    assert page.status_code == 200 and f"ht{tag}" in page.text and "Remove account" not in page.text
    token = c.cookies.get("snx_csrf").split(".")[0]
    r = c.post(f"/admin/accounts/user/{target_id}/edit",
               data={"csrf_token": token, "full_name": "Via Web", "username": f"ht{tag}", "contact": ""})
    assert r.status_code == 303
    r = c.post(f"/admin/accounts/user/{target_id}/remove", data={"csrf_token": token, "reason": "trying it"},
               headers={"Accept": "application/json"})
    assert r.status_code == 403                                                  # edit grant is not remove
    with session_scope() as db:
        assert db.get(User, target_id).full_name == "Via Web"
        assert db.get(User, target_id).status == Status.ACTIVE


# ------------------------------------------------------------------ sabong idempotency replay

def test_sabong_reused_key_on_a_different_bet_is_refused(world):
    from app.db.models import Side
    from app.services import betting
    w = world
    kw = dict(fight_id=w.games[0].id, side=Side.MERON, stake_cents=10_000, settings=w.store,
              player_id=w.player.id, idempotency_key=f"sab-{w.tag}")
    first = betting.place_bet(w.db, w.admin, **kw)
    assert betting.place_bet(w.db, w.admin, **kw).id == first.id
    with pytest.raises(Conflict):
        betting.place_bet(w.db, w.admin, **{**kw, "side": Side.WALA})
    with pytest.raises(Conflict):
        betting.place_bet(w.db, w.admin, **{**kw, "stake_cents": 20_000})
    with pytest.raises(Conflict):
        betting.place_bet(w.db, w.admin, **{**kw, "fight_id": w.games[1].id})
