"""Live credit (/_live/wallet) and the optimized brand logo."""
import os
import re

from sqlalchemy import select

from app.db.models import Role, User
from app.db.session import session_scope
from app.services import accounts, ledger
from tests.test_web import login

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static", "img", "brand")


def _credit(cents: int):
    """Move credit into / out of the admin's wallet the way a load or a cash-out does."""
    with session_scope() as db:
        admin = db.scalar(select(User).where(User.role == Role.ADMIN))
        ledger.post(db, kind="request.in", entries=[(accounts.gcash_clearing(db), +cents), (accounts.wallet_for_user(db, admin), -cents)],
                    actor=admin, memo="test credit")


def test_wallet_endpoint_follows_credit_changes_without_a_refresh():
    c, _ = login("admin")
    first = c.get("/_live/wallet")
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    d = first.json()
    assert isinstance(d["balance_cents"], int) and d["balance"].startswith("₱") and isinstance(d["pending"], int)
    assert c.get("/_live/wallet", headers={"If-None-Match": first.headers["ETag"]}).status_code == 304   # idle poll = headers only

    _credit(5_000)                                                            # credit arrives
    up = c.get("/_live/wallet", headers={"If-None-Match": first.headers["ETag"]})
    assert up.status_code == 200 and up.json()["balance_cents"] == d["balance_cents"] + 5_000
    _credit(-5_000)                                                           # and leaves again
    down = c.get("/_live/wallet", headers={"If-None-Match": up.headers["ETag"]})
    assert down.status_code == 200 and down.json()["balance_cents"] == d["balance_cents"]


def test_header_and_endpoint_agree_and_the_page_is_wired_for_live_updates():
    c, _ = login("admin")
    html = c.get("/dashboard").text
    live = c.get("/_live/wallet").json()
    chip = re.search(r'<span class="bal" data-wallet-balance data-cents="(-?\d+)"[^>]*>([^<]+)</span>', html)
    assert chip and int(chip.group(1)) == live["balance_cents"] and chip.group(2) == live["balance"]
    assert "data-wallet-balance>" in html                                    # the wallet menu's number is marked too


def test_wallet_endpoint_needs_a_login():
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).get("/_live/wallet", follow_redirects=False).status_code in (302, 303, 401, 403)


def test_licence_logo_is_optimized_and_sits_in_the_header_not_the_brand():
    sizes = {n: os.path.getsize(os.path.join(STATIC, n)) for n in os.listdir(STATIC)}
    assert {"tl-logo-64.webp", "tl-logo-128.webp", "tl-logo-256.webp", "tl-logo-128.png"} <= set(sizes)
    assert all(v < 20_000 for v in sizes.values()), sizes                    # the original was 114 KB
    c, _ = login("admin")
    page = c.get("/dashboard").text
    head = page.split('<header class="topbar">', 1)[1].split("</header>", 1)[0]
    assert head.count('class="license-logo"') == 1 and "/static/img/brand/tl-logo-64.webp 64w" in head   # in the header, once
    assert head.index('class="license-logo"') < head.index("<h1>")            # next to the page title
    brand = page.split('<div class="brand">', 1)[1].split("</div>", 1)[0]
    assert "/static/icons/icon.svg" in brand and "tl-logo" not in brand       # the site brand is untouched
    from fastapi.testclient import TestClient
    from app.main import app
    login_page = TestClient(app).get("/login").text
    assert "/static/icons/icon.svg" in login_page and "tl-logo" not in login_page
