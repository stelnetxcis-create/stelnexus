"""License transparency: Variables -> License drives the header logo link, the footer line and the public /license page."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.settings_registry import SettingsStore
from app.db.models import Role, User
from app.db.session import session_scope
from tests.test_web import login

DEFAULTS = {"license.holder": "", "license.number": "", "license.valid_until": "", "license.verify_url": "",
            "license.statement": "", "license.issuer": "Philippine Charity Sweepstakes Office (PCSO)",
            "license.notice": "Only for players of legal age. Please play responsibly."}


def _set(**kw):
    with session_scope() as db:
        admin = db.scalar(select(User).where(User.role == Role.ADMIN))
        store = SettingsStore(db)
        for key, value in kw.items():
            store.set("license." + key, value, admin.id)


@pytest.fixture(autouse=True)
def _reset():
    yield
    with session_scope() as db:
        admin = db.scalar(select(User).where(User.role == Role.ADMIN))
        for key, value in DEFAULTS.items():
            SettingsStore(db).set(key, value, admin.id)


def _anon():
    from app.main import app
    return TestClient(app)


def _header(html: str) -> str:
    return html.split('<header class="topbar">', 1)[1].split("</header>", 1)[0]


def test_without_a_license_number_the_logo_is_a_plain_badge_and_the_page_is_hidden():
    assert _anon().get("/license", follow_redirects=False).status_code == 404
    c, _ = login("admin")
    page = c.get("/dashboard").text
    assert '<span class="license-logo"' in _header(page) and 'href="/license"' not in page
    assert "Only for players of legal age" in page                          # the responsible-play notice is still shown
    assert "License No." not in page                                        # and no license claim is made up


def test_license_details_show_in_the_header_footer_login_and_public_page():
    _set(number="PCSO-123-456", holder="Example Gaming Corp.", valid_until="June 30, 2027",
         verify_url="https://www.pcso.gov.ph/", statement="We operate under a PCSO license.")
    c, _ = login("admin")
    page = c.get("/dashboard").text
    head = _header(page)
    assert '<a class="license-logo" href="/license"' in head and "License No. PCSO-123-456" in head
    assert 'class="foot-license"' in page and "Licensed by Philippine Charity Sweepstakes Office (PCSO)" in page

    pub = _anon().get("/license")                                           # no login needed: it is for everyone
    assert pub.status_code == 200
    for text in ("PCSO-123-456", "Example Gaming Corp.", "June 30, 2027", "We operate under a PCSO license.",
                 "Philippine Charity Sweepstakes Office (PCSO)", "Only for players of legal age"):
        assert text in pub.text
    assert 'href="https://www.pcso.gov.ph/" target="_blank" rel="noopener noreferrer"' in pub.text
    assert "/static/img/brand/tl-logo-128.png" in pub.text
    assert 'href="/license"' in _anon().get("/login").text                  # visible before signing in


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "http://insecure.example.com", "data:text/html,x", "ftp://x.example.com",
                                 "https://has space.example.com"])
def test_only_https_verify_links_are_ever_shown(bad):
    _set(number="X-1", verify_url=bad)
    assert "Verify this license" not in _anon().get("/license").text


def test_license_text_is_escaped():
    _set(number="<b>1</b>", holder="<script>alert(1)</script>", statement="<img src=x onerror=alert(1)>")
    html = _anon().get("/license").text
    assert "<script>alert(1)</script>" not in html and "<img src=x" not in html and "&lt;script&gt;" in html
    c, _ = login("admin")
    assert "<b>1</b>" not in c.get("/dashboard").text


def test_admin_can_edit_the_license_group_on_the_variables_page():
    from app.core.settings_registry import DEFS
    keys = {d.key for d in DEFS if d.group == "License"}
    assert keys == set(DEFAULTS)
