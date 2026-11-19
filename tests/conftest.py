"""Test setup: an isolated SQLite file, set BEFORE the app is imported."""
import os
import tempfile

_dir = tempfile.mkdtemp(prefix="stelnexus-test-")
os.environ.update({
    "ENV": "dev",
    "DATABASE_URL": f"sqlite:///{_dir}/test.db",
    "COOKIE_SECURE": "false",
    "COOKIE_SAMESITE": "lax",
    "SECRET_KEY": "test-secret-key-test-secret-key-0123456789",
})

import datetime as dt  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.core.settings_registry import SettingsStore  # noqa: E402
from app.db.migrate import upgrade  # noqa: E402
from app.db.models import EventStatus, RequestDirection, Role, Status, User  # noqa: E402
from app.db.session import engine, session_scope  # noqa: E402
from app.services import accounts, agents, cashdesk, events, players  # noqa: E402

PASSWORD = "Test-Passw0rd-2026!"


@pytest.fixture(scope="session", autouse=True)
def _schema():
    upgrade(engine)
    with session_scope() as db:
        accounts.ensure_system_accounts(db)
        admin = User(username="admin", password_hash=hash_password(PASSWORD), role=Role.ADMIN,
                     full_name="Admin", ref_code=agents._unique_ref(db), status=Status.ACTIVE,
                     must_change_password=False)
        db.add(admin)
        db.flush()
        accounts.wallet_for_user(db, admin)
        accounts.commission_for_user(db, admin)


@pytest.fixture(autouse=True)
def _empty_gcash_pool():
    """The pool is shared state: a teller made by one test would otherwise route (and limit) the
    GCash loads of every later test. Each test starts with no pooled numbers (= legacy routing)."""
    from sqlalchemy import update
    from app.db.models import CashRequest, GcashAccount, GcashAdjustment
    with session_scope() as db:
        db.execute(update(CashRequest).where(CashRequest.gcash_account_id.is_not(None))
                   .values(gcash_account_id=None))
        db.query(GcashAdjustment).delete()
        db.query(GcashAccount).delete()
    yield


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """The whole suite shares one client IP; without this, enough logins across files trip the
    30-per-5-minutes login limit and a later, unrelated test fails to sign in."""
    from app.core import ratelimit
    ratelimit._hits.clear()
    yield


def load_credit(db, admin, store, player, cents):
    """Fund a player the only supported way: GCash load request -> admin approval."""
    req = cashdesk.create_request(db, admin, direction=RequestDirection.IN, amount_cents=cents,
                                  settings=store, player_id=player.id, method="gcash",
                                  reference="GC-TEST-0001")
    cashdesk.review(db, admin, request_id=req.id, approve=True, settings=store)
    return req


@pytest.fixture()
def db():
    with session_scope() as session:
        yield session


@pytest.fixture()
def world(db):
    """Admin, a funded online player, and an OPEN event of 6 games (unique per test).

    Money enters only the online way: a GCash load request approved by the admin."""
    store = SettingsStore(db)
    admin = db.scalar(select(User).where(User.username == "admin"))
    tag = f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-9:]
    player = players.register_online_player(
        db, admin, username=f"wp{tag}", password=PASSWORD, full_name="Player",
        birth_date=dt.date(1990, 1, 1), gcash_number="09171234567", settings=store)
    player.kyc_verified = True
    load_credit(db, admin, store, player, 10_000_000)
    event = events.create_event(db, admin, title=f"Event {tag}", event_date=dt.date.today(),
                                settings=store, fight_count=6)
    events.set_event_status(db, admin, event.id, EventStatus.OPEN)
    return type("World", (), {"db": db, "store": store, "admin": admin, "player": player,
                              "event": event,
                              "games": events.fights_of(db, event.id), "tag": tag})
