"""DEV 'view as a player': the preview must follow the Admin across every page a player has (not fall back to
the Admin console after one click), show only the player's rights, wallet and tickets, and stay read-only."""
import datetime as dt
import re

import pytest
from sqlalchemy import select

from app.core.settings_registry import SettingsStore
from app.db.models import BolaKind, DrawStatus, Role, Side, User
from app.db.session import session_scope
from app.services import agents, betting, bola, locations, players
from app.web.deps import VIEWAS_COOKIE, make_viewas_cookie_value
from tests.conftest import PASSWORD, fund_user, load_credit
from tests.test_web import login

# What a Sub Agent / Admin sees in the sidebar and a Player must never see.
ADMIN_ONLY_LINKS = ('title="Admin Panel"', 'title="Staff Panel"', 'title="Users"', 'title="Players"',
                    'title="Cancel Ticket"', 'title="Bola Reports"', 'title="Bola Cancelled"', 'title="Dashboard"',
                    'title="Store"')
PLAYER_MENU = ('title="Events"', 'title="Fight View"', 'title="Game History"', 'title="Betting List"',
               'title="Bola"', 'title="Bola Tickets"')


@pytest.fixture(scope="module")
def scene():
    tag = f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-8:]
    with session_scope() as db:
        store = SettingsStore(db)
        admin = db.scalar(select(User).where(User.username == "admin"))
        store.set("bola.enabled", "true", admin.id)
        master = agents.create_agent(db, admin, username=f"pm{tag}", password=PASSWORD, role=Role.MASTER,
                                     full_name="PM", settings=store)
        agent = agents.create_agent(db, master, username=f"pa{tag}", password=PASSWORD, role=Role.AGENT,
                                    full_name="PA", settings=store)
        sub = agents.create_agent(db, agent, username=f"ps{tag}", password=PASSWORD, role=Role.SUB_AGENT,
                                  full_name="PS", settings=store)
        stella = players.register_online_player(
            db, sub, username=f"stella{tag}", password=PASSWORD, full_name="Stella Test",
            birth_date=dt.date(1990, 1, 1), gcash_number="09171234567", settings=store, agent_id=sub.id)
        other = players.register_online_player(
            db, sub, username=f"other{tag}", password=PASSWORD, full_name="Other Person",
            birth_date=dt.date(1990, 1, 1), gcash_number="09171234568", settings=store, agent_id=sub.id)
        for p in (stella, other):
            p.kyc_verified = True
        load_credit(db, admin, store, stella, 500_000)
        load_credit(db, admin, store, other, 500_000)
        from app.services import events
        from app.db.models import EventStatus
        event = events.create_event(db, admin, title=f"PrevEvent {tag}", event_date=dt.date.today(),
                                    settings=store, fight_count=3)
        events.set_event_status(db, admin, event.id, EventStatus.OPEN)
        fight = events.fights_of(db, event.id)[0]
        event_id, fight_id = event.id, fight.id
        mine = betting.place_bet(db, sub, fight_id=fight.id, side=Side.MERON, stake_cents=10_000,
                                 settings=store, player_id=stella.id, idempotency_key=f"pv-mine-{tag}")
        theirs = betting.place_bet(db, sub, fight_id=fight.id, side=Side.WALA, stake_cents=20_000,
                                   settings=store, player_id=other.id, idempotency_key=f"pv-theirs-{tag}")
        loc = locations.create_location(db, admin, name=f"PrevArea {tag}")
        draw = bola.create_draw(db, admin, location_id=loc.id, draw_date=dt.date.today(), slot=1, settings=store,
                                scheduled_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2),
                                status=DrawStatus.OPEN)
        # Number-game tickets are always the logged-in account's own bet (player_id only exists on old rows), so
        # the Sub Agent's own ticket is somebody else's ticket as far as the previewed player is concerned.
        fund_user(db, admin, sub, 500_000)
        bsub = bola.place_bola_bet(db, sub, draw_id=draw.id, kind=BolaKind.PAIR, numbers=[3, 4],
                                   tumbok_cents=5000, sahod_cents=5000, settings=store)
        for u in (master, agent, sub):
            u.must_change_password = False
        db.flush()
        ids = dict(event=event_id, fight=fight_id, tag=tag, stella=stella.id, other=other.id, sub=sub.id, loc=loc.id, draw=draw.id,
                   mine=mine.code, mine_id=mine.id, theirs=theirs.code, theirs_id=theirs.id,
                   bsub=bsub.code, bsub_id=bsub.id,
                   stella_name=stella.username, master=master.username)
    yield ids
    with session_scope() as db:                       # an active area makes later sabong fixtures demand a location
        db.get(type(loc), ids["loc"]).is_active = False


@pytest.fixture()
def admin_c(scene):
    c = login("admin")[0]
    yield c
    c.cookies.delete(VIEWAS_COOKIE)


def csrf(client, path="/dashboard"):
    return re.search(r'name="csrf-token" content="([^"]+)"', client.get(path).text).group(1)


def preview(c, scene):
    r = c.post("/dev/view-as", data={"csrf_token": csrf(c), "player_id": str(scene["stella"])})
    assert r.status_code == 303 and r.headers["location"] == "/events"
    return r


def test_dev_menu_lists_players_and_chip_starts_the_preview(admin_c, scene):
    html = admin_c.get("/dashboard").text
    assert 'name="role" value="player"' in html and f'value="{scene["stella"]}"' in html
    r = admin_c.post("/dev/view-as", data={"csrf_token": csrf(admin_c), "role": "player"})
    assert r.status_code == 303 and r.headers["location"] == "/events"


def test_every_player_menu_page_stays_in_the_player_preview(admin_c, scene):
    c = admin_c
    preview(c, scene)
    for path in ("/events", "/change_name", "/history", "/betlogs", "/bola", "/bola/betlogs", "/bola/results",
                 "/wallet"):
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code)
        assert f'Previewing as <b>{scene["stella_name"]}</b> (Player)' in r.text, path
        for link in ADMIN_ONLY_LINKS:
            assert link not in r.text, (path, link)
        for link in PLAYER_MENU:
            assert link in r.text, (path, link)
        assert 'class="menu dev-menu"' in r.text, path            # the Admin can always get back out


def test_top_bar_shows_the_players_credit_not_the_admins(admin_c, scene):
    c = admin_c
    admin_html = c.get("/events").text
    preview(c, scene)
    html = c.get("/events").text
    assert "₱5,000.00" in html or "₱4,900.00" in html or "₱4,700.00" in html     # Stella: 5,000 less her two bets
    bal = lambda h: re.search(r'class="bal"[^>]*>([^<]+)<', h).group(1)
    assert bal(html) != bal(admin_html)


def test_player_sees_only_their_own_tickets(admin_c, scene):
    c = admin_c
    preview(c, scene)
    page = c.get("/betlogs").text
    assert scene["mine"] in page and scene["theirs"] not in page
    r = c.get("/bola/betlogs")
    assert r.status_code == 200 and scene["bsub"] not in r.text
    assert c.get(f"/tickets/{scene['theirs_id']}").status_code == 403
    assert c.get(f"/bola/tickets/{scene['bsub_id']}").status_code == 403


def test_player_rights_are_below_a_sub_agent(admin_c, scene):
    c = admin_c
    preview(c, scene)
    for path in ("/dashboard", "/admin", "/agents", "/clp", "/staff", "/cancelT", "/commissions", "/referrals",
                 "/bola/reports"):
        assert c.get(path).status_code == 403, path
    assert 'dev-perms' in c.get("/events").text


def test_preview_is_read_only(admin_c, scene):
    """Same rule as every other preview: the write is refused (redirect + message) and nothing is saved."""
    from sqlalchemy import func
    from app.db.models import BolaTicket, Event, Ticket

    def counts():
        with session_scope() as db:
            return tuple(db.scalar(select(func.count(m.id))) for m in (Event, Ticket, BolaTicket))

    c = admin_c
    preview(c, scene)
    before = counts()
    token = csrf(c, "/events")
    r = c.post("/events/create", data={"csrf_token": token, "title": "nope", "event_date": "2026-12-01",
                                       "fight_count": "2"})
    assert r.status_code == 303 and counts() == before
    r = c.post("/bola/bets/place", data={"csrf_token": token, "draw_id": str(scene["draw"])})
    assert r.status_code == 303 and counts() == before


def test_exit_preview_restores_the_admin_console(admin_c, scene):
    c = admin_c
    preview(c, scene)
    assert c.post("/dev/view-as/exit", data={"csrf_token": csrf(c, "/events")}).status_code == 303
    c.cookies.delete(VIEWAS_COOKIE)
    html = c.get("/dashboard").text
    assert 'title="Admin Panel"' in html and "Previewing as" not in html


def test_player_preview_cookie_cannot_be_used_by_anyone_else(scene):
    with session_scope() as db:
        admin_id = db.scalar(select(User.id).where(User.username == "admin"))
    m = login(scene["master"])[0]
    m.cookies.set(VIEWAS_COOKIE, make_viewas_cookie_value(admin_id, player_id=scene["stella"]))
    html = m.get("/dashboard").text
    assert "Previewing as" not in html and 'title="Dashboard"' in html


def test_preview_never_writes(admin_c, scene):
    """Browsing as a player must not create a wallet, ledger row or audit entry beyond the single view_as one."""
    from app.db.models import Account, AuditLog, LedgerEntry
    from sqlalchemy import func
    with session_scope() as db:
        before = (db.scalar(select(func.count(Account.id))), db.scalar(select(func.count(LedgerEntry.id))))
    c = admin_c
    preview(c, scene)
    for path in ("/events", "/change_name", "/betlogs", "/bola", "/bola/betlogs", "/wallet", "/history"):
        c.get(path)
    with session_scope() as db:
        after = (db.scalar(select(func.count(Account.id))), db.scalar(select(func.count(LedgerEntry.id))))
    assert before == after


def test_the_player_can_bet_so_the_bet_panel_and_slip_show_with_their_own_credit(admin_c, scene):
    """A player has bets.place / bola.place and credit, so the live bet panel (Fight View) and the Bola bet slip
    must be there in the preview. Pressing Place is refused only because the preview is read-only."""
    c = admin_c
    preview(c, scene)
    page = c.get(f"/change_name?event_id={scene['event']}&fight_id={scene['fight']}").text
    assert 'aria-label="Bet panel"' in page
    assert "Place a bet" in page
    bola_page = c.get(f"/bola/draws/{scene['draw']}").text
    assert "data-bola-slip" in bola_page
    token = csrf(c, "/events")
    r = c.post("/bets/live/place", data={"csrf_token": token, "event_id": str(scene["event"])},
               headers={"Accept": "application/json"})
    assert r.status_code in (303, 403)
    with session_scope() as db:                       # and nothing was booked
        from app.db.models import Ticket
        from sqlalchemy import func
        assert db.scalar(select(func.count(Ticket.id)).where(Ticket.fight_id == scene["fight"])) == 2


_ROUTE_MODULES = ("routes_auth routes_dashboard routes_agents routes_players routes_fights routes_bets routes_money "
                  "routes_admin routes_referrals routes_cuts routes_extra routes_wallet routes_bola routes_live "
                  "routes_store routes_staff").split()
_SKIP = ("/chat/stream", "/login", "/register", "/qr.png", ".csv", "/proof")     # streams, public pages, binary / files


def _get_paths():
    import importlib
    paths = set()
    for name in _ROUTE_MODULES:
        for r in importlib.import_module(f"app.web.{name}").router.routes:
            if "GET" in (getattr(r, "methods", None) or ()) and not any(x in r.path for x in _SKIP):
                paths.add(r.path)
    return sorted(paths)


def test_crawl_every_page_as_the_previewed_player_nothing_breaks_and_credit_is_always_shown(admin_c, scene):
    """The sweep behind 'fix all related': every GET page, opened as the previewed player, must answer with a
    page or a clean refusal (never a crash), show the player's credit in the top bar on every full page, and
    never show an Admin-only menu entry."""
    ids = {"event_id": scene["event"], "fight_id": scene["fight"], "draw_id": scene["draw"],
           "ticket_id": scene["mine_id"], "player_id": scene["stella"], "agent_id": scene["sub"]}
    c = admin_c
    preview(c, scene)
    problems, pages = [], 0
    for tpl in _get_paths():
        path = tpl
        for key, val in ids.items():
            path = path.replace("{" + key + "}", str(val))
        if "{" in path:
            problems.append(f"{tpl}: no sample id for its parameter")
            continue
        r = c.get(path)
        if r.status_code >= 500:
            problems.append(f"{path}: {r.status_code}")
            continue
        if r.status_code == 200 and "text/html" in r.headers.get("content-type", "") and 'class="menu dev-menu"' in r.text:
            pages += 1
            if 'class="bal"' not in r.text:
                problems.append(f"{path}: the player's credit is missing from the top bar")
            for link in ADMIN_ONLY_LINKS:
                if link in r.text:
                    problems.append(f"{path}: shows an Admin-only menu entry {link}")
    assert pages >= 8, f"only {pages} full pages rendered - the crawl is not covering the player's menu"
    assert not problems, "\n".join(problems)
