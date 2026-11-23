"""Phone navigation: portrait phones use the bottom bar (last tab "More" opens a sheet), the sidebar is for desktop /
landscape. No duplicated profile/account blocks in the sidebar. Server-rendered structure only; look is css/mobilenav.css."""
from tests.test_web import login


def test_sidebar_has_no_duplicated_account_blocks():
    c, _ = login("admin")
    page = c.get("/dashboard").text
    assert '<nav class="nav">' in page                              # other tests look for this exact tag
    assert 'class="nav-me"' not in page and 'class="nav-account"' not in page
    assert "#i-home" in page


def test_bottom_bar_has_more_tab_and_settings_is_in_account_menu():
    c, _ = login("admin")
    page = c.get("/dashboard").text
    assert "data-open-more" in page and 'aria-controls="more-sheet"' in page and ">More<" in page
    assert 'href="/preferences"' in page                                 # sounds + animations live on the Settings page
    assert 'aria-controls="sidebar"' in page                         # hamburger still exists for landscape/desktop
    assert "mobilenav" in page or "site." in page                    # served through the site bundles


def test_no_account_blocks_for_signed_out_pages():
    from fastapi.testclient import TestClient
    from app.main import app
    page = TestClient(app).get("/login").text
    assert 'class="nav-account"' not in page and 'class="nav-me"' not in page and "data-open-more" not in page


def test_more_tab_uses_its_own_icon_and_bar_is_customisable_in_js():
    c, _ = login("admin")
    page = c.get("/dashboard").text
    assert '<symbol id="i-more"' in page and "#i-more" in page.split('data-open-more')[1][:300]
    js = open("app/static/js/mobilenav.js", encoding="utf-8").read()
    assert "sn.bar.v1" in js and "'/dashboard', '/games', '/records'" in js    # default pins: Home, All games, Records


def test_payout_card_can_be_hidden_from_the_dock_row():
    js = open("app/static/js/dock.js", encoding="utf-8").read()
    assert 'data-dk="pay"' in js and "snx.pbs.hidden" in js
    c, _ = login("admin")
    from app.db.session import SessionLocal
    from app.db.models import Fight
    with SessionLocal() as db:
        f = db.query(Fight).first()
    if f is None:
        return
    page = c.get(f"/change_name?event_id={f.event_id}&fight_id={f.id}")
    assert page.status_code == 200 and "data-pbs-hide" in page.text
