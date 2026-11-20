"""Teams: the sports team catalog (the NBA first).

This is preparation only. The 30 NBA teams are registered with their logo, conference and division so that
games, odds and betting can point at them later; nothing in the app uses a team yet, and no game exists.

* ``ensure_teams`` seeds the catalog. It only ever INSERTS missing teams (matched by sport + code), so an
  Admin's on/off choice is never overwritten, and it is safe to run on every start.
* The Admin switches a team on or off in Admin Panel -> Teams (``set_active``).
* Logos are the files in ``app/static/img/teams/<sport>/<code>.svg`` (lower-case code). They are shown with
  ``<img>``, never inlined, so their own CSS classes cannot leak into the page.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.errors import Forbidden, NotFound
from app.core.rbac import Perm, has_perm
from app.db.models import Team, User

IMG_DIR = Path(__file__).resolve().parents[1] / "static" / "img" / "teams"

# (nba team id, code, city, name, conference, division)
NBA_TEAMS: tuple[tuple[int, str, str, str, str, str], ...] = (
    # East / Atlantic
    (1610612738, "BOS", "Boston", "Celtics", "East", "Atlantic"),
    (1610612751, "BKN", "Brooklyn", "Nets", "East", "Atlantic"),
    (1610612752, "NYK", "New York", "Knicks", "East", "Atlantic"),
    (1610612755, "PHI", "Philadelphia", "76ers", "East", "Atlantic"),
    (1610612761, "TOR", "Toronto", "Raptors", "East", "Atlantic"),
    # East / Central
    (1610612741, "CHI", "Chicago", "Bulls", "East", "Central"),
    (1610612739, "CLE", "Cleveland", "Cavaliers", "East", "Central"),
    (1610612765, "DET", "Detroit", "Pistons", "East", "Central"),
    (1610612754, "IND", "Indiana", "Pacers", "East", "Central"),
    (1610612749, "MIL", "Milwaukee", "Bucks", "East", "Central"),
    # East / Southeast
    (1610612737, "ATL", "Atlanta", "Hawks", "East", "Southeast"),
    (1610612766, "CHA", "Charlotte", "Hornets", "East", "Southeast"),
    (1610612748, "MIA", "Miami", "Heat", "East", "Southeast"),
    (1610612753, "ORL", "Orlando", "Magic", "East", "Southeast"),
    (1610612764, "WAS", "Washington", "Wizards", "East", "Southeast"),
    # West / Northwest
    (1610612743, "DEN", "Denver", "Nuggets", "West", "Northwest"),
    (1610612750, "MIN", "Minnesota", "Timberwolves", "West", "Northwest"),
    (1610612760, "OKC", "Oklahoma City", "Thunder", "West", "Northwest"),
    (1610612757, "POR", "Portland", "Trail Blazers", "West", "Northwest"),
    (1610612762, "UTA", "Utah", "Jazz", "West", "Northwest"),
    # West / Pacific
    (1610612744, "GSW", "Golden State", "Warriors", "West", "Pacific"),
    (1610612746, "LAC", "LA", "Clippers", "West", "Pacific"),
    (1610612747, "LAL", "Los Angeles", "Lakers", "West", "Pacific"),
    (1610612756, "PHX", "Phoenix", "Suns", "West", "Pacific"),
    (1610612758, "SAC", "Sacramento", "Kings", "West", "Pacific"),
    # West / Southwest
    (1610612742, "DAL", "Dallas", "Mavericks", "West", "Southwest"),
    (1610612745, "HOU", "Houston", "Rockets", "West", "Southwest"),
    (1610612763, "MEM", "Memphis", "Grizzlies", "West", "Southwest"),
    (1610612740, "NOP", "New Orleans", "Pelicans", "West", "Southwest"),
    (1610612759, "SAS", "San Antonio", "Spurs", "West", "Southwest"),
)


def logo_file(code: str) -> str:
    return f"{code.lower()}.svg"


def ensure_teams(db: Session) -> int:
    """Register any missing NBA team. Existing rows are left exactly as they are. Returns how many were added."""
    have = set(db.scalars(select(Team.code).where(Team.sport == "nba")).all())
    added = 0
    for ext_id, code, city, name, conference, division in NBA_TEAMS:
        if code in have:
            continue
        db.add(Team(sport="nba", code=code, ext_id=ext_id, city=city, name=name,
                    conference=conference, division=division, logo=logo_file(code)))
        added += 1
    if added:
        db.flush()
    return added


def list_teams(db: Session, sport: str | None = None, *, active_only: bool = False) -> list[Team]:
    stmt = select(Team).order_by(Team.sport, Team.conference, Team.division, Team.city)
    if sport:
        stmt = stmt.where(Team.sport == sport)
    if active_only:
        stmt = stmt.where(Team.is_active.is_(True))
    return list(db.scalars(stmt).all())


def grouped(teams: list[Team]) -> list[tuple[str, str, list[Team]]]:
    """(conference, division, teams) in table order, for the Admin page."""
    out: list[tuple[str, str, list[Team]]] = []
    for t in teams:
        key = (t.conference or "", t.division or "")
        if not out or (out[-1][0], out[-1][1]) != key:
            out.append((key[0], key[1], []))
        out[-1][2].append(t)
    return out


def set_active(db: Session, actor: User, team_id: int, active: bool) -> Team:
    if not has_perm(actor, Perm.TEAMS_MANAGE):
        raise Forbidden("You cannot change teams.")
    row = db.get(Team, team_id)
    if row is None:
        raise NotFound("Team not found.")
    row.is_active = bool(active)
    db.flush()
    audit.record(db, actor, "team.activate" if active else "team.deactivate",
                 entity_type="team", entity_id=row.id, after={"sport": row.sport, "code": row.code})
    return row
