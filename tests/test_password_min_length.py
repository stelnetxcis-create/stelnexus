"""The "Minimum password length" setting (Admin > Settings > Security) is THE rule.

It used to be a second, stricter rule on top of the PASSWORD_MIN_LENGTH config default (12): lowering the setting
to 8 still answered "Use at least 12 characters." on every account-creating screen. These tests pin each place
that takes a new password. The other password rules (three character classes, not the username, not a common
password) are untouched.
"""
import datetime as dt

import pytest
from sqlalchemy import select

from app.core.errors import ValidationError
from app.core.security import password_problems
from app.core.settings_registry import SettingsStore
from app.db.models import Role, User
from app.services import agents, auth, players
from tests.conftest import PASSWORD

EIGHT = "Ab3$kLm9"        # exactly 8 characters, 4 character classes
SEVEN = "Ab3$kLm"
KEY = "security.password_min_length"


@pytest.fixture()
def admin_store(db):
    store = SettingsStore(db)
    admin = db.scalar(select(User).where(User.username == "admin"))
    original = store.get(KEY)
    yield admin, store
    store.set(KEY, str(original), admin.id)


def _tag():
    return f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-9:]


def test_helper_follows_the_argument_not_the_config_default():
    assert password_problems(EIGHT, "someone", min_length=8) == []
    assert password_problems(SEVEN, "someone", min_length=8) == ["Use at least 8 characters."]
    assert "Use at least 12 characters." in password_problems(EIGHT, "someone")        # no setting given: config default
    assert password_problems(EIGHT, "someone", min_length=20) == ["Use at least 20 characters."]


def test_other_rules_still_apply_at_a_low_minimum():
    assert password_problems("abcdefgh", "someone", min_length=8)                      # only one character class
    assert password_problems("Pass-word1", "pass", min_length=8)                       # contains the username


def test_creating_an_agent_with_the_lowered_minimum(db, admin_store):
    admin, store = admin_store
    store.set(KEY, "8", admin.id)
    user = agents.create_agent(db, admin, username=f"ag{_tag()}", password=EIGHT, role=Role.MASTER,
                               full_name="Agent", settings=store)
    assert user.id
    with pytest.raises(ValidationError, match="at least 8 characters"):
        agents.create_agent(db, admin, username=f"ag{_tag()}x", password=SEVEN, role=Role.MASTER,
                            full_name="Agent", settings=store)


def test_raising_the_minimum_is_enforced_too(db, admin_store):
    admin, store = admin_store
    store.set(KEY, "16", admin.id)
    with pytest.raises(ValidationError, match="at least 16 characters"):
        agents.create_agent(db, admin, username=f"ag{_tag()}", password=PASSWORD[:14] + "9", role=Role.MASTER,
                            full_name="Agent", settings=store)


def test_registering_a_player_and_resetting_and_changing_a_password(db, admin_store):
    admin, store = admin_store
    store.set(KEY, "8", admin.id)
    tag = _tag()
    player = players.register_online_player(
        db, admin, username=f"pl{tag}", password=EIGHT, full_name="Player", birth_date=dt.date(1990, 1, 1),
        gcash_number="09171234567", settings=store)
    assert player.id
    with pytest.raises(ValidationError, match="at least 8 characters"):
        players.register_online_player(
            db, admin, username=f"pm{tag}", password=SEVEN, full_name="Player", birth_date=dt.date(1990, 1, 1),
            gcash_number="09171234567", settings=store)

    agent = agents.create_agent(db, admin, username=f"ar{tag}", password=PASSWORD, role=Role.MASTER,
                                full_name="Agent", settings=store)
    agents.reset_password(db, admin, agent.id, "Zz9#Yy8!", settings=store)             # 8 characters: accepted
    with pytest.raises(ValidationError, match="at least 8 characters"):
        agents.reset_password(db, admin, agent.id, "Zz9#Yy8", settings=store)
    auth.change_password(db, agent, current="Zz9#Yy8!", new="Qq7@Ww6!", settings=store)
    with pytest.raises(ValidationError) as err:
        auth.change_password(db, agent, current="Qq7@Ww6!", new="Qq7@Ww6", settings=store)
    assert str(err.value).count("at least") == 1                                        # one message, not two
