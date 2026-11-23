"""Phone navigation: the Menu carries who you are, where to go, the account items and Sign out; the top bar and the
bottom bar stay compact. Server-rendered structure only; the look is in css/mobilenav.css."""
from tests.test_web import login


def test_menu_has_account_card_account_links_and_sign_out():
    c, _ = login("admin")
    page = c.get("/dashboard").text
    assert 'class="nav-me"' in page and "Admin" in page.split('class="nav-me"')[1][:400]
    assert '<nav class="nav">' in page                              # other tests look for this exact tag
    side = page.split('id="sidebar"')[1].split("</aside>")[0]
    assert 'class="nav-account"' in side and "Profile &amp; security" in side
    assert "data-sound-toggle" in side and 'action="/logout"' in side
    assert "#i-home" in side and 'aria-expanded="false"' in page


def test_tabbar_and_assets_are_wired():
    c, _ = login("admin")
    page = c.get("/dashboard").text
    assert 'aria-controls="sidebar"' in page and ">Menu<" in page
    assert "mobilenav" in page or "site." in page                    # served through the site bundles


def test_account_section_is_missing_for_signed_out_pages():
    from fastapi.testclient import TestClient
    from app.main import app
    page = TestClient(app).get("/login").text
    assert 'class="nav-account"' not in page and 'class="nav-me"' not in page
