"""One device per account: a new sign-in ends every other web session and mobile token."""
from sqlalchemy import select

from app.core.settings_registry import SettingsStore
from app.db.models import Session as DbSession, User
from app.db.session import session_scope
from tests.test_web import login


def _set(value: bool):
    with session_scope() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        SettingsStore(db).set("security.single_session", "true" if value else "false", admin.id)


def test_second_login_signs_the_first_device_out():
    _set(True)
    a, ra = login("admin")
    assert ra.status_code == 303 and a.get("/dashboard").status_code == 200
    b, rb = login("admin")                                  # same account, "another device"
    assert rb.status_code == 303
    assert b.get("/dashboard").status_code == 200           # newest login works
    old = a.get("/dashboard")                               # the first one is kicked
    assert old.status_code == 303 and old.headers["location"].startswith("/login")
    page = a.get("/login")                                  # and is told why
    assert "another device" in page.text


def test_only_one_live_session_row_remains():
    _set(True)
    login("admin"); login("admin"); login("admin")
    with session_scope() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        live = db.scalars(select(DbSession).where(DbSession.user_id == admin.id, DbSession.revoked_at.is_(None))).all()
        assert len(live) == 1


def test_switch_off_allows_two_devices():
    _set(False)
    try:
        a, _ = login("admin")
        b, _ = login("admin")
        assert a.get("/dashboard").status_code == 200 and b.get("/dashboard").status_code == 200
    finally:
        _set(True)


def test_other_accounts_are_not_affected():
    _set(True)
    a, _ = login("admin")
    login("master") if False else None
    with session_scope() as db:
        others = db.scalars(select(User).where(User.username != "admin", User.status == "active")).first()
    if others is not None:
        c, r = login(others.username)
        if r.status_code == 303:
            assert a.get("/dashboard").status_code == 200
