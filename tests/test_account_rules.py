"""Account guardrails: username length + uniqueness, Philippine mobile check, one Gmail per account (information only)."""
import pytest
from sqlalchemy import select

from app.core.errors import Conflict, ValidationError
from app.core.identity import check_username, email_key, gmail_parts, normalize_ph_mobile
from app.core.settings_registry import SettingsStore
from app.db.models import Player, Role, User
from app.db.session import session_scope
from app.services import agents, identity, players, referrals
from tests.conftest import PASSWORD
from tests.test_web import login


# ------------------------------------------------------------------ username rules (pure)

@pytest.mark.parametrize("name", ["maria_santos", "juan.delacruz", "abcdefg", "A1b2C3d", "user-name-1"])
def test_good_usernames(name):
    assert check_username(name) == name


@pytest.mark.parametrize("name,why", [
    ("abcdef", "at least 7"), ("", "at least 7"), ("  abc  ", "at least 7"),            # six characters is not enough
    ("a" * 33, "at most 32"),
    ("_maria_santos", "start and end"), ("maria_santos.", "start and end"), ("maria santos", "letters, numbers"),
    ("maria@home1", "letters, numbers"), ("maria__santos", "two dots, dashes"), ("ma..ria-santos", "two dots, dashes"),
    ("マリア1234567", "letters, numbers"),
])
def test_bad_usernames_say_what_to_change(name, why):
    with pytest.raises(ValidationError, match=why):
        check_username(name)


def test_minimum_length_is_a_setting_and_reserved_names_only_block_public_signup():
    assert check_username("abcde", min_length=5) == "abcde"
    with pytest.raises(ValidationError, match="reserved"):
        check_username("administrator", public_signup=True)
    with pytest.raises(ValidationError, match="reserved"):
        check_username("sup.port", min_length=7, public_signup=True)                   # dots do not hide it
    assert check_username("administrator") == "administrator"                            # the Admin can still create one


# ------------------------------------------------------------------ Philippine mobile numbers (pure)

@pytest.mark.parametrize("raw", ["09171234567", "+639171234567", "639171234567", "9171234567", "0917 123 4567", "0917-123-4567",
                                 "(0917) 123-4567"])
def test_valid_ph_numbers_are_stored_as_09(raw):
    assert normalize_ph_mobile(raw) == "09171234567"


@pytest.mark.parametrize("raw", ["", "abc", "0917123456", "091712345678", "08171234567", "+441234567890", "63917123456",
                                 "09111111111", "09123456789", "09987654321", "09121212121", "09000000000"])
def test_wrong_or_made_up_numbers_are_refused(raw):
    with pytest.raises(ValidationError):
        normalize_ph_mobile(raw)


# ------------------------------------------------------------------ Gmail (pure)

def test_gmail_is_canonicalised_so_tricks_do_not_make_a_second_account():
    base = gmail_parts("juan.delacruz@gmail.com")[1]
    assert base == "juandelacruz@gmail.com"
    for variant in ("JuanDelaCruz@gmail.com", "juan.dela.cruz@gmail.com", "juandelacruz+bets@gmail.com", "j.u.a.n.d.e.l.a.c.r.u.z@googlemail.com"):
        assert gmail_parts(variant)[1] == base and email_key(gmail_parts(variant)[1]) == email_key(base)
    assert gmail_parts("  Juan.DelaCruz@GMAIL.com ")[0] == "juan.delacruz@gmail.com"


@pytest.mark.parametrize("raw", ["", "juan", "juan@yahoo.com", "juan@gmail.co", "a@b@gmail.com", "short@gmail.com", "..juandelacruz@gmail.com",
                                 "juandelacruz.@gmail.com", "juan dela@gmail.com", "x" * 31 + "@gmail.com", "+tag@gmail.com"])
def test_not_a_gmail_is_refused(raw):
    with pytest.raises(ValidationError):
        gmail_parts(raw)


# ------------------------------------------------------------------ uniqueness across users and players (database)

def _admin(db):
    return db.scalar(select(User).where(User.role == Role.ADMIN))


def _mk(db, admin, name, **kw):
    return agents.create_agent(db, admin, username=name, password=PASSWORD, role=Role.MASTER, full_name=name.upper(),
                               settings=SettingsStore(db), **kw)


def test_a_username_cannot_be_reused_in_any_case_or_by_a_player():
    with session_scope() as db:
        admin, st = _admin(db), SettingsStore(db)
        tag = __import__("uuid").uuid4().hex[:8]
        user = _mk(db, admin, f"Maria{tag}")
        for again in (f"Maria{tag}", f"maria{tag}", f"MARIA{tag}"):
            with pytest.raises(Conflict, match="already taken"):
                _mk(db, admin, again)
        import datetime as dt
        with pytest.raises(Conflict, match="already taken"):                       # a player cannot take a login account's name
            players.register_online_player(db, admin, username=f"maria{tag}", password=PASSWORD, full_name="X",
                                           birth_date=dt.date(1990, 1, 1), gcash_number="09171234567", settings=st)
        assert identity.username_taken(db, f"MARIA{tag}") and not identity.username_taken(db, f"MARIA{tag}", user_id=user.id)


def test_new_accounts_need_a_username_above_six_characters():
    with session_scope() as db:
        admin = _admin(db)
        with pytest.raises(ValidationError, match="at least 7"):
            _mk(db, admin, "short6")
        SettingsStore(db).set("account.username_min_length", "5", admin.id)
        try:
            assert _mk(db, admin, "abc" + __import__("uuid").uuid4().hex[:3]).username
        finally:
            SettingsStore(db).set("account.username_min_length", "7", admin.id)


def test_one_gmail_per_account_even_with_dots_plus_tags_and_across_players():
    import datetime as dt
    import uuid
    with session_scope() as db:
        admin, st = _admin(db), SettingsStore(db)
        tag = uuid.uuid4().hex[:6]
        first = _mk(db, admin, f"owner{tag}", email=f"Pat.Santos{tag}@gmail.com")
        assert first.email_key and identity.masked_gmail(first.email_enc).endswith("@gmail.com")
        for variant in (f"patsantos{tag}@gmail.com", f"pat.santos{tag}+x@gmail.com", f"PATSANTOS{tag}@googlemail.com"):
            with pytest.raises(Conflict, match="already used"):
                _mk(db, admin, f"other{uuid.uuid4().hex[:6]}", email=variant)
            with pytest.raises(Conflict, match="already used"):                    # and a player cannot take it either
                players.register_online_player(db, admin, username=f"plyr{uuid.uuid4().hex[:6]}", password=PASSWORD, full_name="P",
                                               birth_date=dt.date(1990, 1, 1), gcash_number="09171234567", settings=st, email=variant)
        second = _mk(db, admin, f"third{tag}", email=f"someone.else{tag}@gmail.com")
        assert second.email_key != first.email_key


def test_the_gmail_is_stored_hashed_and_encrypted_not_as_plain_text():
    import uuid
    with session_scope() as db:
        tag = uuid.uuid4().hex[:6]
        user = _mk(db, _admin(db), f"hidden{tag}", email=f"secret.person{tag}@gmail.com")
        assert len(user.email_key) == 64 and "secret" not in user.email_key
        assert f"secret.person{tag}" not in (user.email_key or "")


# ------------------------------------------------------------------ the sign-up page, end to end

def _code(role="master"):
    with session_scope() as db:
        admin = _admin(db)
        return referrals.create_code(db, admin, role=role, ttl_minutes=60).code


def _signup(c, code, **over):
    import re
    html = c.get(f"/register?code={code}").text
    page = c.post("/register/code", data={"code": code, "csrf_token": re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)})
    tok = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    data = {"csrf_token": tok, "code": code, "full_name": "Pat Santos", "username": "patsantos1", "contact": "09171234567",
            "email": "pat.santos.signup@gmail.com", "password": "Correct-Horse-Battery-9!", "password2": "Correct-Horse-Battery-9!"}
    data.update(over)
    return c.post("/register", data=data, follow_redirects=False)


def test_signup_page_shows_the_gmail_field_and_rejects_bad_values():
    from fastapi.testclient import TestClient

    from app.main import app
    import re
    code = _code()
    c = TestClient(app, follow_redirects=False, base_url="http://127.0.0.1:8000")
    html = c.get(f"/register?code={code}").text
    tok = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    page = c.post("/register/code", data={"code": code, "csrf_token": tok}).text
    assert 'name="email"' in page and "not</b> used to sign in" in page and 'minlength="7"' in page
    for bad, msg in (({"username": "short"}, "at least 7"), ({"contact": "09111111111"}, "real number"),
                     ({"email": "pat@yahoo.com"}, "Gmail"), ({"email": ""}, "Gmail address is required")):
        r = _signup(c, code, **bad)
        assert r.status_code == 400 and msg in r.text, (bad, r.status_code)
    ok = _signup(c, code, username="patsignup_ok1", email="pat.signup.ok@gmail.com")
    assert ok.status_code == 303
    assert "/login" in ok.headers["location"]
    again = _signup(c, _code(), username="patsignup_two", email="PATSIGNUPOK@gmail.com")             # same mailbox, different spelling
    assert again.status_code == 400 and "already used" in again.text
    same_name = _signup(c, _code(), username="PATSIGNUP_OK1", email="another.person.x1@gmail.com")
    assert same_name.status_code == 400 and "already taken" in same_name.text


def test_a_bad_signup_does_not_burn_the_code():
    from fastapi.testclient import TestClient

    from app.main import app
    code = _code()
    c = TestClient(app, follow_redirects=False, base_url="http://127.0.0.1:8000")
    assert _signup(c, code, username="burn1").status_code == 400
    assert _signup(c, code, username="burnfree_ok2", email="burn.free.ok2@gmail.com").status_code == 303


def test_reserved_names_are_blocked_on_signup_but_not_for_the_admin():
    from fastapi.testclient import TestClient

    from app.main import app
    r = _signup(TestClient(app, follow_redirects=False, base_url="http://127.0.0.1:8000"), _code(), username="administrator", email="reserved.try1@gmail.com")
    assert r.status_code == 400 and "reserved" in r.text


def test_admin_can_set_a_gmail_but_not_one_that_is_taken_and_old_contacts_do_not_block_edits():
    import uuid
    tag = uuid.uuid4().hex[:6]
    with session_scope() as db:
        admin = _admin(db)
        a = _mk(db, admin, f"edit_a{tag}", email=f"edit.a{tag}x@gmail.com")
        b = _mk(db, admin, f"edit_b{tag}")
        b.contact = "call after 5pm"                                     # an old, free-text contact
        from app.services import account_admin
        account_admin.edit_user(db, admin, b.id, full_name="B Person")                        # does not trip over it
        account_admin.edit_user(db, admin, b.id, contact="call after 5pm", email=f"edit.b{tag}x@gmail.com")
        with pytest.raises(Conflict, match="already used"):
            account_admin.edit_user(db, admin, b.id, email=f"editA{tag}X@gmail.com")
        with pytest.raises(ValidationError, match="Philippine mobile"):
            account_admin.edit_user(db, admin, b.id, contact="12345")
        account_admin.edit_user(db, admin, a.id, email=f"edit.a{tag}x@gmail.com")             # re-saving your own Gmail is fine
        account_admin.edit_user(db, admin, b.id, contact="0917 765 4321")
        assert b.contact == "09177654321"


def test_accounts_page_shows_a_masked_gmail():
    import uuid
    tag = uuid.uuid4().hex[:6]
    with session_scope() as db:
        _mk(db, _admin(db), f"shown{tag}", email=f"visible.person{tag}@gmail.com")
    c, _ = login("admin")
    page = c.get("/admin/accounts").text
    assert "Gmail (information only)" in page and "v***" in page and f"visible.person{tag}" not in page


def test_two_people_grabbing_the_same_name_at_once_get_a_clear_message_and_keep_the_code(monkeypatch):
    from sqlalchemy.exc import IntegrityError
    code = _code()

    def boom(*a, **k):
        raise IntegrityError("insert", {}, Exception("duplicate key"))
    monkeypatch.setattr(agents, "create_agent", boom)
    with session_scope() as db:
        with pytest.raises(Conflict, match="just taken"):
            referrals.redeem(db, code, username="racer_name1", password=PASSWORD, full_name="R", contact="09171234567",
                             settings=SettingsStore(db), email="racer.mail.one@gmail.com")
    with session_scope() as db:
        assert referrals.lookup_valid(db, code).used_at is None                  # the code is still usable
