"""Admin/Pawn create Cavo & Kubrador directly, the Games lobby, result pictures, staff chat scope,
history location, and who may declare."""
import base64
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import ratelimit
from app.core.errors import Forbidden, ValidationError
from app.core.rbac import Perm, creatable_roles, permissions_of
from app.db.models import Fight, FightStatus, Role, User
from app.main import app
from app.services import agents, events, locations, proofs, referrals, settlement
from tests.conftest import PASSWORD
from tests.test_network import chain  # noqa: F401  (fixture)

@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """These tests sign in many times from one test IP; never let that eat the login budget of other files."""
    ratelimit._hits.clear()
    yield
    ratelimit._hits.clear()


# a real 1x1 PNG
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def _login(username):
    c = TestClient(app, follow_redirects=False, base_url="http://127.0.0.1:8000")
    c.get("/login")
    token = c.cookies.get("snx_csrf").split(".")[0]
    assert c.post("/login", data={"username": username, "password": PASSWORD, "csrf_token": token}).status_code == 303
    return c


def _csrf(c):
    return c.cookies.get("snx_csrf").split(".")[0]


def _mk(w, role, name, actor=None, **kw):
    u = agents.create_agent(w.db, actor or w.admin, username=f"{name}{w.tag}", password=PASSWORD, role=role,
                            full_name=name, settings=w.store, **kw)
    u.must_change_password = False
    return u


def _pawn(w, *grants, name="pw"):
    p = _mk(w, Role.PAWN, name)
    agents.set_pawn_grants(w.db, w.admin, p.id, list(grants))
    w.db.flush(); w.db.commit()
    return p


def _fresh_game(w):
    """An open, empty game of this test's event."""
    w.db.expire_all()
    return next(g for g in events.fights_of(w.db, w.event.id) if g.status == FightStatus.OPEN)


# ------------------------------------------------ admin / pawn create Cavo + Kubrador directly
def test_admin_creates_cavo_and_kubrador_directly(chain):
    w = chain
    cavo = agents.create_agent(w.db, w.admin, username=f"dc{w.tag}", password=PASSWORD, role=Role.CAVO,
                               full_name="DC", settings=w.store, parent_id=w.master.id)
    assert cavo.parent_id == w.master.id and cavo.created_by_id == w.admin.id and cavo.has_wallet
    kub = agents.create_agent(w.db, w.admin, username=f"dk{w.tag}", password=PASSWORD, role=Role.KUBRADOR,
                              full_name="DK", settings=w.store, parent_id=cavo.id)
    assert kub.parent_id == cavo.id and kub.role == Role.KUBRADOR


def test_upline_must_be_the_right_tier(chain):
    w = chain
    with pytest.raises(ValidationError):        # a Cavo cannot sit under a Cavo
        agents.create_agent(w.db, w.admin, username=f"x1{w.tag}", password=PASSWORD, role=Role.CAVO,
                            full_name="X", settings=w.store, parent_id=w.cavo.id)
    with pytest.raises(ValidationError):        # a Kubrador cannot sit under a Master
        agents.create_agent(w.db, w.admin, username=f"x2{w.tag}", password=PASSWORD, role=Role.KUBRADOR,
                            full_name="X", settings=w.store, parent_id=w.master.id)
    with pytest.raises(ValidationError):        # and the upline must exist
        agents.create_agent(w.db, w.admin, username=f"x3{w.tag}", password=PASSWORD, role=Role.KUBRADOR,
                            full_name="X", settings=w.store, parent_id=999_999)


def test_pawn_creates_accounts_only_when_granted(chain):
    w = chain
    plain = _pawn(w, name="pl")
    with pytest.raises(Forbidden):
        agents.create_agent(w.db, plain, username=f"p1{w.tag}", password=PASSWORD, role=Role.CAVO,
                            full_name="P", settings=w.store, parent_id=w.master.id)
    assert creatable_roles(plain) == []

    allowed = _pawn(w, "agents:create", name="pa")
    assert Perm.AGENTS_CREATE in permissions_of(allowed)
    assert creatable_roles(allowed) == [Role.CAVO, Role.KUBRADOR]
    cavo = agents.create_agent(w.db, allowed, username=f"p2{w.tag}", password=PASSWORD, role=Role.CAVO,
                               full_name="P", settings=w.store, parent_id=w.master.id)
    kub = agents.create_agent(w.db, allowed, username=f"p3{w.tag}", password=PASSWORD, role=Role.KUBRADOR,
                              full_name="P", settings=w.store, parent_id=w.cavo.id)
    assert cavo.created_by_id == allowed.id and kub.parent_id == w.cavo.id
    for role in (Role.MASTER, Role.DECLARATOR, Role.PAWN, Role.ADMIN):
        with pytest.raises(Forbidden):
            agents.create_agent(w.db, allowed, username=f"p4{role.value}{w.tag}", password=PASSWORD, role=role,
                                full_name="P", settings=w.store)


def test_master_still_cannot_create_a_kubrador_directly(chain):
    w = chain
    assert creatable_roles(w.master) == [Role.CAVO, Role.DECLARATOR]
    with pytest.raises(Forbidden):
        agents.create_agent(w.db, w.master, username=f"mk{w.tag}", password=PASSWORD, role=Role.KUBRADOR,
                            full_name="MK", settings=w.store)


def test_users_page_create_form_web(chain):
    w = chain
    w.db.commit()
    a = _login("admin")
    html = a.get("/agents").text
    assert 'name="parent_id"' in html and '<option value="cavo">' in html and '<option value="kubrador">' in html
    r = a.post("/agents/create", data={"csrf_token": _csrf(a), "role": "kubrador", "parent_id": str(w.cavo.id),
                                       "full_name": "Web K", "username": f"wk{w.tag}", "password": PASSWORD})
    assert r.status_code == 303
    w.db.expire_all()
    made = w.db.scalar(select(User).where(User.username == f"wk{w.tag}"))
    assert made is not None and made.role == Role.KUBRADOR and made.parent_id == w.cavo.id

    p = _login(_pawn(w, "agents:create", name="pw2").username)
    page = p.get("/agents")
    assert page.status_code == 200 and "My credit" not in page.text      # a Pawn has no wallet
    assert p.post(f"/agents/{w.cavo.id}/update", data={"csrf_token": _csrf(p), "full_name": "no"}).status_code == 303
    w.db.expire_all()
    assert w.db.get(User, w.cavo.id).full_name == "C"                    # create is granted, edit is not


# ------------------------------------------------ staff chat: Admin and granted Pawns only
def test_staff_chat_is_admin_and_granted_pawns_only(chain):
    w = chain
    plain = _pawn(w, name="nc")
    chat = _pawn(w, "chat", name="yc")
    w.master.must_change_password = False
    w.db.commit()
    assert _login("admin").get("/chat").status_code == 200
    assert _login(chat.username).get("/chat").status_code == 200
    for who in (w.master.username, plain.username):
        c = _login(who)
        assert c.get("/chat").status_code == 403, who
        assert c.get("/chat/messages?after=0", headers={"Accept": "application/json"}).status_code == 403
        assert c.post("/chat/send", data={"csrf_token": _csrf(c), "body": "hi"}).status_code in (303, 403)
    assert 'href="/chat"' in _login(chat.username).get("/change_name").text
    assert 'href="/chat"' not in _login(plain.username).get("/change_name").text


# ------------------------------------------------ who may declare
def test_only_declare_roles_can_declare_cavo_and_kubrador_are_players(chain):
    w = chain
    for role in (Role.CAVO, Role.KUBRADOR):
        assert Perm.FIGHTS_DECLARE not in permissions_of(w.cavo if role == Role.CAVO else w.kub)
    for who in (w.admin, _mk(w, Role.DECLARATOR, "dc"), _pawn(w, name="dp")):
        assert Perm.FIGHTS_DECLARE in permissions_of(who)
    assert not {Perm.FIGHTS_DECLARE, Perm.FIGHTS_REDECLARE} & permissions_of(w.master)   # Master cannot declare
    w.cavo.must_change_password = False; w.kub.must_change_password = False
    w.db.commit()
    game = _fresh_game(w)
    for u in (w.cavo, w.kub):
        c = _login(u.username)
        assert "Declare &amp; settle" not in c.get(f"/change_name?event_id={w.event.id}&fight_id={game.id}").text
        r = c.post(f"/fights/{game.id}/declare", data={"csrf_token": _csrf(c), "result": "meron"})
        assert r.status_code == 303
    w.db.expire_all()
    assert w.db.get(Fight, game.id).status == FightStatus.OPEN and w.db.get(Fight, game.id).result is None


# ------------------------------------------------ Admin referral codes can make every account type
def test_admin_referral_codes_make_any_account_type(chain):
    w = chain
    assert referrals.invite_roles(w.admin) == ["master", "cavo", "kubrador", "player"]
    assert referrals.invite_roles(w.master) == ["cavo"] and referrals.invite_roles(w.kub) == ["player"]

    code = referrals.create_code(w.db, w.admin, ttl_minutes=60)            # default stays Master
    assert code.role == "master" and code.parent_id is None
    m = referrals.redeem(w.db, code.code, username=f"am{w.tag}", password=PASSWORD, full_name="AM",
                         contact=None, settings=w.store)
    assert m.role == Role.MASTER and m.parent_id == w.admin.id

    code = referrals.create_code(w.db, w.admin, ttl_minutes=60, role="cavo", parent_id=w.master.id)
    c = referrals.redeem(w.db, code.code, username=f"ac{w.tag}", password=PASSWORD, full_name="AC",
                         contact=None, settings=w.store)
    assert c.role == Role.CAVO and c.parent_id == w.master.id

    code = referrals.create_code(w.db, w.admin, ttl_minutes=60, role="kubrador", parent_id=w.cavo.id)
    k = referrals.redeem(w.db, code.code, username=f"ak{w.tag}", password=PASSWORD, full_name="AK",
                         contact=None, settings=w.store)
    assert k.role == Role.KUBRADOR and k.parent_id == w.cavo.id

    code = referrals.create_code(w.db, w.admin, ttl_minutes=60, role="player", parent_id=w.kub.id)
    p = referrals.redeem(w.db, code.code, username=f"ap{w.tag}", password=PASSWORD, full_name="AP",
                         contact=None, settings=w.store, birth_date=dt.date(1990, 1, 1), gcash_number="09171234567")
    assert p.agent_id == w.kub.id


def test_admin_referral_upline_must_fit_and_others_cannot_widen(chain):
    w = chain
    with pytest.raises(ValidationError):                                     # a Kubrador cannot sit under a Master
        referrals.create_code(w.db, w.admin, ttl_minutes=60, role="kubrador", parent_id=w.master.id)
    with pytest.raises(Forbidden):
        referrals.create_code(w.db, w.master, ttl_minutes=60, role="master")
    with pytest.raises(Forbidden):
        referrals.create_code(w.db, w.cavo, ttl_minutes=60, role="cavo")
    with pytest.raises(Forbidden):
        referrals.create_code(w.db, w.kub, ttl_minutes=60, role="kubrador")
    assert referrals.create_code(w.db, w.master, ttl_minutes=60, role="cavo").role == "cavo"


def test_admin_referrals_page_offers_the_role_picker(chain):
    w = chain
    w.db.commit()
    html = _login("admin").get("/referrals").text
    assert 'name="role"' in html and 'name="parent_id"' in html and "/static/js/referrals.js" in html
    w.master.must_change_password = False
    w.db.commit()
    m = _login(w.master.username).get("/referrals").text
    assert 'name="parent_id"' not in m and "Cavo" in m


# ------------------------------------------------ result pictures
def _declare(c, game, picture=None, name="r.png", mime="image/png", result="meron"):
    files = {"proof": (name, picture, mime)} if picture is not None else None
    return c.post(f"/fights/{game.id}/declare", data={"csrf_token": _csrf(c), "result": result}, files=files)


def test_pawn_must_upload_a_picture_to_declare(chain):
    w = chain
    pawn = _pawn(w, name="pp")
    c = _login(pawn.username)
    game = _fresh_game(w)
    page = c.get(f"/change_name?event_id={w.event.id}&fight_id={game.id}").text
    assert 'type="file" name="proof"' in page and "required" in page.split('name="proof"')[1].split(">")[0]

    assert _declare(c, game).status_code == 303                          # no picture: refused
    w.db.expire_all()
    assert w.db.get(Fight, game.id).status == FightStatus.OPEN

    assert _declare(c, game, b"<svg onload=alert(1)/>", "x.png", "image/png").status_code == 303   # not an image
    assert _declare(c, game, b"GIF89a....", "x.gif", "image/gif").status_code == 303
    w.db.expire_all()
    assert w.db.get(Fight, game.id).status == FightStatus.OPEN and not w.db.get(Fight, game.id).proof_file

    assert _declare(c, game, PNG).status_code == 303                     # with a real picture: declared
    w.db.expire_all()
    done = w.db.get(Fight, game.id)
    assert done.status in (FightStatus.SETTLED, FightStatus.VOID) and done.result is not None
    assert done.proof_file and done.proof_mime == "image/png" and done.proof_by_id == pawn.id
    assert proofs.path_of(done).read_bytes() == PNG

    shown = c.get(f"/change_name?event_id={w.event.id}&fight_id={game.id}").text
    assert f'src="/fights/{game.id}/proof"' in shown
    img = c.get(f"/fights/{game.id}/proof")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png" and img.content == PNG


def test_admin_and_declarator_may_skip_the_picture_but_it_shows_if_given(chain):
    w = chain
    dec = _mk(w, Role.DECLARATOR, "dcp")
    w.db.commit()
    a, d = _login("admin"), _login(dec.username)
    g1, g2 = _fresh_game(w), None
    assert _declare(a, g1).status_code == 303
    w.db.expire_all()
    assert w.db.get(Fight, g1.id).result is not None and not w.db.get(Fight, g1.id).proof_file
    assert a.get(f"/fights/{g1.id}/proof").status_code == 404

    g2 = _fresh_game(w)
    assert 'name="proof"' in d.get(f"/change_name?event_id={w.event.id}&fight_id={g2.id}").text
    assert _declare(d, g2).status_code == 303                          # a Declarator needs no picture
    w.db.expire_all()
    assert w.db.get(Fight, g2.id).result is not None and not w.db.get(Fight, g2.id).proof_file

    g3 = _fresh_game(w)
    assert _declare(d, g3, PNG, result="wala").status_code == 303      # ...but may add one
    w.db.expire_all()
    assert w.db.get(Fight, g3.id).proof_file
    assert f'src="/fights/{g3.id}/proof"' in a.get(f"/change_name?event_id={w.event.id}&fight_id={g3.id}").text

    # Admin can add or replace a picture on a finished game; Cavo cannot
    r = a.post(f"/fights/{g1.id}/proof", data={"csrf_token": _csrf(a)}, files={"proof": ("p.png", PNG, "image/png")})
    assert r.status_code == 303
    w.db.expire_all()
    assert w.db.get(Fight, g1.id).proof_file
    w.cavo.must_change_password = False; w.db.commit()
    c = _login(w.cavo.username)
    r = c.post(f"/fights/{g1.id}/proof", data={"csrf_token": _csrf(c)}, files={"proof": ("p.png", PNG, "image/png")})
    assert r.status_code == 303
    w.db.expire_all()
    assert w.db.get(Fight, g1.id).proof_file.startswith(f"g{g1.id}-")    # unchanged by the Cavo's attempt


def test_master_cannot_declare(chain):
    w = chain
    w.master.must_change_password = False
    w.db.commit()
    m = _login(w.master.username)
    game = _fresh_game(w)
    assert "Declare &amp; settle" not in m.get(f"/change_name?event_id={w.event.id}&fight_id={game.id}").text
    assert _declare(m, game, PNG).status_code == 303
    w.db.expire_all()
    assert w.db.get(Fight, game.id).status == FightStatus.OPEN and w.db.get(Fight, game.id).result is None


def test_pawn_proof_requirement_can_be_switched_off(chain):
    w = chain
    c = _login(_pawn(w, name="po").username)
    w.store.set("fight.pawn_proof_required", "false", None)
    w.db.commit()                                   # the web request reads it through its own connection
    try:
        game = _fresh_game(w)
        assert _declare(c, game).status_code == 303
        w.db.expire_all()
        assert w.db.get(Fight, game.id).result is not None
    finally:
        w.store.set("fight.pawn_proof_required", "true", None)
        w.db.commit()


def test_multipart_post_without_csrf_token_is_refused(chain):
    w = chain
    w.db.commit()
    c = _login("admin")
    game = _fresh_game(w)
    r = c.post(f"/fights/{game.id}/declare", data={"result": "meron"}, files={"proof": ("p.png", PNG, "image/png")})
    assert r.status_code == 403
    w.db.expire_all()
    assert w.db.get(Fight, game.id).status == FightStatus.OPEN


def test_picture_cannot_be_read_without_signing_in(chain):
    w = chain
    w.db.commit()
    game = _fresh_game(w)
    c = _login("admin")
    _declare(c, game, PNG)
    anon = TestClient(app, follow_redirects=False, base_url="http://127.0.0.1:8000")
    assert anon.get(f"/fights/{game.id}/proof").status_code in (303, 401, 403)


# ------------------------------------------------ lobby + history location
def test_games_lobby_boxes_are_clickable(chain):
    w = chain
    w.db.commit()
    a = _login("admin")
    html = a.get("/events").text
    assert "lobby-card" in html and f'href="/events/{w.event.id}"' in html and w.event.title in html
    assert "Main event" not in html and "Main Event" not in html
    detail = a.get(f"/events/{w.event.id}").text
    assert detail.count('class="game-tile ') == w.event.fight_count      # the wrapper is game-tiles
    assert f"/change_name?event_id={w.event.id}&amp;fight_id={w.games[0].id}" in detail or \
        f"/change_name?event_id={w.event.id}&fight_id={w.games[0].id}" in detail
    picker = a.get("/change_name").text                                  # no event chosen: the same boxes
    assert "lobby-card" in picker and f'href="/change_name?event_id={w.event.id}"' in picker
    assert 'game-tile' in a.get(f"/change_name?event_id={w.event.id}").text


def test_lobby_stats_count_games(chain):
    w = chain
    game = _fresh_game(w)
    settlement.declare(w.db, w.admin, fight_id=game.id, result=events.resolve_result(game, "meron"),
                       settings=w.store)
    stats = events.lobby_stats(w.db, [w.event.id])[w.event.id]
    assert stats["total"] == 6 and stats["settled"] + stats["void"] == 1 and stats["live"] == 2
    assert stats["pct"] == round(100 / 6)


def test_history_shows_the_location(chain):
    w = chain
    venue = locations.create_location(w.db, w.admin, name=f"Coliseum {w.tag}", city="Lipa City")
    ev = events.create_event(w.db, w.admin, title=f"Derby {w.tag}", event_date=dt.date.today(), settings=w.store,
                             fight_count=2, location_id=venue.id)
    events.set_event_status(w.db, w.admin, ev.id, events.EventStatus.OPEN)
    game = events.fights_of(w.db, ev.id)[0]
    settlement.declare(w.db, w.admin, fight_id=game.id, result=events.resolve_result(game, "wala"), settings=w.store)
    w.db.commit()
    html = _login("admin").get("/history").text
    assert "<th>Location</th>" in html and f"Coliseum {w.tag}" in html and "Lipa City" in html



# ------------------------------------------------ venue pictures (keep these LAST: they commit locations,
# and a test event cannot be created without a location once one exists)
def _bare_event(db, store, admin, tag):
    from app.db.models import Location  # noqa: F401
    venue = locations.create_location(db, admin, name=f"Temp Venue {tag}")
    ev = events.create_event(db, admin, title=f"Bare {tag}", event_date=dt.date.today(), settings=store,
                             fight_count=2, location_id=venue.id)
    ev.location_id = None                     # an event that predates venues
    db.flush()
    return ev


def test_sample_venues_have_pictures_and_seeding_is_repeatable(db):
    from app.core.rbac import Perm  # noqa: F401
    from app.core.settings_registry import SettingsStore
    from pathlib import Path
    store = SettingsStore(db)
    admin = db.scalar(select(User).where(User.username == "admin"))
    ev = _bare_event(db, store, admin, f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-9:])

    first = locations.seed_samples(db, admin)
    assert len(first["created"]) == 5 and first["events_attached"] >= 1
    db.refresh(ev)
    assert ev.location_id is not None and ev.location.image in locations.VENUE_IMAGES
    names = {n for n, *_ in locations.SAMPLE_LOCATIONS}
    assert "Lipa Arena" in names and all("Batangas" in c for _, c, _, _ in locations.SAMPLE_LOCATIONS)
    root = Path(locations.__file__).resolve().parents[1] / "static" / "img" / "venues"
    assert all((root / pic).is_file() for pic in locations.VENUE_IMAGES)

    again = locations.seed_samples(db, admin)
    assert again["created"] == [] and again["events_attached"] == 0


def test_lobby_box_uses_the_venue_picture_as_its_background(db):
    from app.core.settings_registry import SettingsStore
    from app.web.deps import templates
    store = SettingsStore(db)
    admin = db.scalar(select(User).where(User.username == "admin"))
    tag = f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-9:]
    with_pic = locations.create_location(db, admin, name=f"Pic Venue {tag}", image="lipa.svg")
    plain = locations.create_location(db, admin, name=f"Plain Venue {tag}")
    a = events.create_event(db, admin, title=f"A {tag}", event_date=dt.date.today(), settings=store,
                            fight_count=2, location_id=with_pic.id)
    b = events.create_event(db, admin, title=f"B {tag}", event_date=dt.date.today(), settings=store,
                            fight_count=2, location_id=plain.id)
    stats = events.lobby_stats(db, [a.id, b.id])
    html = str(templates.env.get_template("_lobby.html").module.event_cards([a, b], stats, "/events/"))
    assert html.count("has-img") == 1
    assert "--card-img: url('/static/img/venues/lipa.svg')" in html


def test_venue_picture_must_be_a_bundled_one_and_old_callers_keep_it(db):
    admin = db.scalar(select(User).where(User.username == "admin"))
    tag = f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-9:]
    with pytest.raises(ValidationError):
        locations.create_location(db, admin, name=f"Bad {tag}", image="../../etc/passwd")
    with pytest.raises(ValidationError):
        locations.create_location(db, admin, name=f"Bad2 {tag}", image="https://evil.example/x.png")
    row = locations.create_location(db, admin, name=f"Keep {tag}", image="tanauan.svg")
    locations.update_location(db, admin, row.id, name=row.name, address=None, city="Tanauan", notes=None,
                              custom_fields=None, default_fight_count=None, default_payout_model=None,
                              default_house_cut_bps=None)                       # no image argument: unchanged
    assert row.image == "tanauan.svg" and row.image_url == "/static/img/venues/tanauan.svg"
    locations.update_location(db, admin, row.id, name=row.name, address=None, city="Tanauan", notes=None,
                              custom_fields=None, default_fight_count=None, default_payout_model=None,
                              default_house_cut_bps=None, image="")
    assert row.image is None


def test_admin_locations_page_has_the_picture_picker():
    html = _login("admin").get("/admin/locations").text
    assert 'name="image"' in html and "Nasugbu beach" in html
