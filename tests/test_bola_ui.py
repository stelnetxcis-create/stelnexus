"""Dev-facing number-game features over HTTP: reports, expenses, CSV, cancel, result photo,
draw times, dashboard card, the number grid, and the JSON API."""
import datetime as dt
import re

from fastapi.testclient import TestClient

from app.core.assets import asset_urls
from tests.test_bola import _web_setup, _web_teardown
from tests.test_web import app, login
from tests.conftest import PASSWORD

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _csrf(c):
    return c.cookies.get("snx_csrf").split(".")[0]


def _place(c, ids, n1, n2, tumbok="7", sahod="0"):
    r = c.post("/bola/bets/place", data={
        "csrf_token": _csrf(c), "draw_id": ids["draw"], "player_id": ids["player"], "kind": "pair",
        "n1": n1, "n2": n2, "tumbok": tumbok, "sahod": sahod, "print": "1"})
    assert r.status_code == 303, r.text[:300]
    return int(r.headers["location"].rsplit("/", 1)[1])


def test_draw_page_has_every_control_the_routes_expect():
    ids = _web_setup()
    try:
        c, _ = login("admin")
        html = c.get(f"/bola/draws/{ids['draw']}").text
        for needle in ("data-bola-grid", 'data-max="40"', "Draw expenses", "Add expense",
                       'enctype="multipart/form-data"', "Report CSV", asset_urls("js", "bola")[0]):
            assert needle in html, needle
        js = c.get("/static/js/bola.js")
        assert js.status_code == 200 and "bola-ball" in js.text
        assert ".bola-ball" in c.get("/static/css/base.css").text
    finally:
        _web_teardown(ids)


def test_expenses_cancel_proof_reports_and_csv_end_to_end():
    ids = _web_setup()
    d = ids["draw"]
    try:
        c, _ = login("admin")
        win = _place(c, ids, "7", "9", tumbok="7")
        gone = _place(c, ids, "11", "12", tumbok="3")

        # cancel one ticket from the draw page's own form
        r = c.post(f"/bola/bets/{gone}/cancel", data={"csrf_token": _csrf(c), "reason": "typo at the desk"})
        assert r.status_code == 303
        assert "canceled" in c.get(f"/bola/tickets/{gone}").text.lower()

        # expenses: add, show, remove
        r = c.post(f"/bola/draws/{d}/expenses", data={"csrf_token": _csrf(c), "label": "Runner pay",
                                                      "amount": "50", "note": "morning"})
        assert r.status_code == 303
        page = c.get(f"/bola/draws/{d}").text
        assert "Runner pay" in page
        eid = int(re.search(r"/bola/expenses/(\d+)/delete", page).group(1))

        # declare WITH a result picture (multipart)
        r = c.post(f"/bola/draws/{d}/declare", data={
            "csrf_token": _csrf(c), "ball_first": "7", "ball_second": "9",
            "reason": "web test, before draw time"}, files={"proof": ("result.png", PNG, "image/png")})
        assert r.status_code == 303, r.text[:300]
        img = c.get(f"/bola/draws/{d}/proof")
        assert img.status_code == 200 and img.headers["content-type"] == "image/png"
        assert "Result picture" in c.get(f"/bola/draws/{d}").text

        # a non-image is refused and the picture is unchanged
        r = c.post(f"/bola/draws/{d}/proof", data={"csrf_token": _csrf(c)},
                   files={"proof": ("x.svg", b"<svg onload=alert(1)>", "image/svg+xml")},
                   headers={"Accept": "application/json"})
        assert r.status_code == 422 or r.status_code == 400
        assert c.get(f"/bola/draws/{d}/proof").content == PNG

        # per-draw CSV: money is exact, the cancelled ticket is not counted as staked
        csv = c.get(f"/bola/draws/{d}/report.csv")
        assert csv.status_code == 200 and "text/csv" in csv.headers["content-type"]
        assert "Total staked,7.00" in csv.text, csv.text
        assert "Paid out,5600.00" in csv.text
        assert "Expenses,50.00" in csv.text and "Runner pay" in csv.text

        # daily + monthly report page render the same figures
        today = dt.date.today().isoformat()
        rep = c.get(f"/bola/reports?date={today}&month={today[:7]}")
        assert rep.status_code == 200
        assert "Daily report" in rep.text and "Monthly report" in rep.text and "Web Area" in rep.text
        # junk query values fall back to today instead of failing
        assert c.get("/bola/reports?date=nope&month=2026-99").status_code == 200

        # removing the expense changes net income
        assert c.post(f"/bola/expenses/{eid}/delete", data={"csrf_token": _csrf(c)}).status_code == 303
        assert "Expenses,0.00" in c.get(f"/bola/draws/{d}/report.csv").text
        assert win
    finally:
        _web_teardown(ids)


def test_required_picture_for_pawn_is_enforced_by_the_helper():
    from app.core.settings_registry import SettingsStore
    from app.db.models import Role
    from app.db.session import session_scope
    from app.services import agents, proofs
    from sqlalchemy import select
    from app.db.models import User
    with session_scope() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        store = SettingsStore(db)
        pawn = agents.create_agent(db, admin, username="uipawn", password=PASSWORD, role=Role.PAWN,
                                   full_name="P", settings=store)
        assert proofs.required_for(pawn, store) is bool(store.get("fight.pawn_proof_required"))
        assert proofs.required_for(admin, store) is False


def test_draw_times_page_saves_edits_and_deletes():
    ids = _web_setup()
    try:
        c, _ = login("admin")
        r = c.post("/admin/bola/schedules/save", data={
            "csrf_token": _csrf(c), "location_id": ids["loc"], "slot": "2", "draw_time": "16:30", "is_active": "1"})
        assert r.status_code == 303
        page = c.get("/admin/bola/schedules").text
        assert "16:30" in page and "Web Area" in page
        sid = int(re.search(r"/admin/bola/schedules/(\d+)/delete", page).group(1))
        # bad input is flashed back, not a 500
        r = c.post("/admin/bola/schedules/save", data={
            "csrf_token": _csrf(c), "location_id": ids["loc"], "slot": "9", "draw_time": "25:99"},
            headers={"Accept": "application/json"})
        assert r.status_code == 422 or r.status_code == 400
        assert c.post(f"/admin/bola/schedules/{sid}/delete", data={"csrf_token": _csrf(c)}).status_code == 303
        assert "16:30" not in c.get("/admin/bola/schedules").text
    finally:
        _web_teardown(ids)


def _staked_today(html: str) -> int:
    """Cents shown in the card's 'Staked today' tile (the card totals every draw of the day,
    including ones other tests left behind, so tests compare before/after, never absolutes)."""
    card = html.split('id="bola-card"')[1].split("Needs attention")[0]
    amount = re.search(r"Staked today</span>\s*<strong[^>]*>[^0-9]*([0-9,]+\.[0-9]{2})", card).group(1)
    return int(round(float(amount.replace(",", "")) * 100))


def test_dashboard_card_counts_todays_draw():
    ids = _web_setup()
    try:
        c, _ = login("admin")
        before_html = c.get("/dashboard").text
        assert 'id="bola-card"' in before_html and "Draws today" in before_html
        before = _staked_today(before_html)
        _place(c, ids, "1", "2", tumbok="10")
        after_html = c.get("/dashboard").text
        assert "Staked today" in after_html
        assert _staked_today(after_html) - before == 1000    # the PHP 10.00 just staked
    finally:
        _web_teardown(ids)


def _active_nav_label(html: str) -> list[str]:
    return re.findall(r'class="nav-link is-active" href="[^"]*" title="([^"]*)"', html)


def test_sidebar_highlights_the_page_you_are_on():
    c, _ = login("admin")
    for path, label in (("/bola", "Bola"), ("/bola/betlogs", "Bola Tickets"),
                        ("/bola/reports", "Bola Reports")):
        assert _active_nav_label(c.get(path).text) == [label], path


# --------------------------------------------------------------------------- JSON API
def _api_token():
    c = TestClient(app)
    tok = c.post("/api/v1/auth/token", json={"username": "admin", "password": PASSWORD}).json()["token"]
    return c, {"Authorization": f"Bearer {tok}"}


def test_api_draws_bet_idempotency_cancel_and_results():
    ids = _web_setup()
    try:
        c, h = _api_token()
        draws = c.get("/api/v1/bola/draws", headers=h).json()
        assert any(x["id"] == ids["draw"] for x in draws["draws"]) and draws["odds"]["max_number"] == 40

        body = {"draw_id": ids["draw"], "kind": "pair", "numbers": [3, 4], "tumbok": "5",
                "player_id": ids["player"], "idempotency_key": "api-test-key-0001"}
        t1 = c.post("/api/v1/bola/bets", json=body, headers=h).json()["ticket"]
        t2 = c.post("/api/v1/bola/bets", json=body, headers=h).json()["ticket"]
        assert t1["id"] == t2["id"] and t1["stake_cents"] == 500

        pv = c.get(f"/api/v1/bola/draws/{ids['draw']}/preview",
                   params={"kind": "casas3", "numbers": "1,2,3", "tumbok": "2", "sahod": "1"}, headers=h).json()
        assert pv["line_count"] == 3 and pv["stake_cents"] == 900

        bad = c.post("/api/v1/bola/bets", json={**body, "numbers": [3, 3], "idempotency_key": "k2"}, headers=h)
        assert bad.status_code in (400, 422)
        assert c.get(f"/api/v1/bola/tickets/by-code/{t1['code']}", headers=h).json()["ticket"]["id"] == t1["id"]

        out = c.post(f"/api/v1/bola/tickets/{t1['id']}/cancel", json={"reason": "api test"}, headers=h).json()
        assert out["ticket"]["status"] == "canceled"
        assert c.get("/api/v1/bola/results", headers=h).status_code == 200
        assert TestClient(app).get("/api/v1/bola/draws").status_code == 401      # no token, no data
    finally:
        _web_teardown(ids)
