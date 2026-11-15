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
from app.db.models import EventStatus, Role, Station, Status, User  # noqa: E402
from app.db.session import engine, session_scope  # noqa: E402
from app.services import accounts, agents, events  # noqa: E402

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


@pytest.fixture()
def db():
    with session_scope() as session:
        yield session


@pytest.fixture()
def world(db):
    """Admin, a teller with a cash station, and an OPEN event of 6 games (unique per test)."""
    store = SettingsStore(db)
    admin = db.scalar(select(User).where(User.username == "admin"))
    tag = f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-9:]
    teller = agents.create_agent(db, admin, username=f"tel{tag}", password=PASSWORD, role=Role.TELLER,
                                 full_name="Teller", settings=store)
    station = Station(code=f"S{tag}", name="Station", user_id=teller.id, is_active=True)
    db.add(station)
    db.flush()
    accounts.cash_for_station(db, station)
    event = events.create_event(db, admin, title=f"Event {tag}", event_date=dt.date.today(),
                                settings=store, fight_count=6)
    events.set_event_status(db, admin, event.id, EventStatus.OPEN)
    return type("World", (), {"db": db, "store": store, "admin": admin, "teller": teller,
                              "station": station, "event": event,
                              "games": events.fights_of(db, event.id), "tag": tag})
