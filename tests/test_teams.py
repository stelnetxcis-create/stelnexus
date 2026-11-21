"""The NBA team catalog: seeded, idempotent, logos present, Admin-only page, on/off survives a re-seed."""
import datetime as dt
import re

import pytest
from sqlalchemy import select

from app.core import rbac
from app.core.errors import Forbidden
from app.core.rbac import Perm, permissions_for
from app.core.settings_registry import SettingsStore
from app.db.models import AuditLog, Role, Team, User
from app.db.session import session_scope
from app.services import agents, teams
from tests.conftest import PASSWORD
from tests.test_web import login


def _tag():
    return f"{dt.datetime.now().timestamp():.6f}".replace(".", "")[-9:]


def _nba(db):
    return list(db.scalars(select(Team).where(Team.sport == "nba")).all())


def test_seed_registers_30_teams_and_is_idempotent():
    with session_scope() as db:
        teams.ensure_teams(db)
        assert teams.ensure_teams(db) == 0                 # second run adds nothing
        rows = _nba(db)
        assert len(rows) == 30
        assert len({t.code for t in rows}) == 30 and len({t.ext_id for t in rows}) == 30
        assert {t.conference for t in rows} == {"East", "West"}
        # 3 divisions per conference, 5 teams each
        for conf in ("East", "West"):
            divs = {}
            for t in rows:
                if t.conference == conf:
                    divs.setdefault(t.division, []).append(t.code)
            assert len(divs) == 3 and all(len(v) == 5 for v in divs.values()), conf
        lakers = next(t for t in rows if t.code == "LAL")
        assert lakers.full_name == "Los Angeles Lakers" and lakers.ext_id == 1610612747


def test_every_team_has_a_clean_logo_file():
    for _ext, code, *_ in teams.NBA_TEAMS:
        path = teams.IMG_DIR / "nba" / teams.logo_file(code)
        assert path.is_file(), code
        text = path.read_text(encoding="utf-8")
        # the league's logos come in different boxes (HOU is portrait, ORL 50x50): each needs a viewBox to scale
        assert text.lstrip().startswith("<svg") and re.search(r'viewBox="0 0 \d+ \d+"', text), code
        # shown with <img>, but still: no script, handlers or outside references
        assert not re.search(r"<script|\son\w+=|href=[\"']https?:", text, re.I), code
    assert len(list((teams.IMG_DIR / "nba").glob("*.svg"))) == 30


def test_switch_off_is_kept_audited_and_admin_only():
    with session_scope() as db:
        teams.ensure_teams(db)
        admin = db.scalar(select(User).where(User.username == "admin"))
        store = SettingsStore(db)
        master = agents.create_agent(db, admin, username="tm" + _tag(), password=PASSWORD, role=Role.MASTER,
                                     full_name="TM", settings=store)
        team = next(t for t in _nba(db) if t.code == "BOS")
        with pytest.raises(Forbidden):
            teams.set_active(db, master, team.id, False)
        teams.set_active(db, admin, team.id, False)
        db.flush()
        assert teams.ensure_teams(db) == 0 and not db.get(Team, team.id).is_active   # a re-seed does not switch it back on
        assert "BOS" not in {t.code for t in teams.list_teams(db, "nba", active_only=True)}
        assert db.scalar(select(AuditLog.id).where(AuditLog.action == "team.deactivate", AuditLog.entity_id == str(team.id)))
        teams.set_active(db, admin, team.id, True)


def test_admin_teams_page():
    with session_scope() as db:
        teams.ensure_teams(db)
        admin = db.scalar(select(User).where(User.username == "admin"))
        master = agents.create_agent(db, admin, username="tp" + _tag(), password=PASSWORD, role=Role.MASTER,
                                     full_name="TP", settings=SettingsStore(db))
        master.must_change_password = False
        mname = master.username
    c, _ = login("admin")
    r = c.get("/admin/teams")
    assert r.status_code == 200
    assert r.text.count('class="team-logo"') == 30 and "Los Angeles Lakers" in r.text
    assert "/static/img/teams/nba/lal.svg" in r.text
    assert c.get("/static/img/teams/nba/lal.svg").status_code == 200
    m, _ = login(mname)
    assert m.get("/admin/teams").status_code == 403         # Master does not get the Admin Panel
    assert Perm.TEAMS_VIEW not in permissions_for(Role.MASTER) and Perm.TEAMS_VIEW in permissions_for(Role.ADMIN)
    assert "teams:view" in {g.key for g in rbac.GRANTS} and "teams:edit" in {g.key for g in rbac.GRANTS}


def test_toggle_button_route():
    with session_scope() as db:
        teams.ensure_teams(db)
        tid = next(t.id for t in _nba(db) if t.code == "MIA")
    c, _ = login("admin")
    token = c.cookies.get("snx_csrf").split(".")[0]
    r = c.post(f"/admin/teams/{tid}/deactivate", data={"csrf_token": token})
    assert r.status_code == 303
    assert 'class="team-tile is-off"' in c.get("/admin/teams").text
    token = c.cookies.get("snx_csrf").split(".")[0]                 # the token rotates after a POST
    c.post(f"/admin/teams/{tid}/bogus", data={"csrf_token": token})      # an unknown action is refused and changes nothing
    assert 'class="team-tile is-off"' in c.get("/admin/teams").text
    token = c.cookies.get("snx_csrf").split(".")[0]
    c.post(f"/admin/teams/{tid}/activate", data={"csrf_token": token})
    assert 'class="team-tile is-off"' not in c.get("/admin/teams").text
