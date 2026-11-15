"""Server-side role & object permissions.

The original panel gated features by *hiding buttons* (`if($_SESSION[UTYPE]=='0')`)
while the AJAX handlers that actually moved money were unverified. In StelNexus
every route -- HTML or JSON -- passes through `require()`, and every object touch
passes through a downline scope check. UI hiding is cosmetic only.
"""
from __future__ import annotations

from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Forbidden
from app.db.models import CHILD_ROLE, Player, PlayerKind, Role, Status, User


class Perm(str, Enum):
    DASHBOARD = "dashboard.view"
    AGENTS_VIEW = "agents.view"
    AGENTS_CREATE = "agents.create"
    AGENTS_EDIT = "agents.edit"
    AGENTS_TRANSFER = "agents.transfer"
    PLAYERS_VIEW = "players.view"
    PLAYERS_CREATE = "players.create"
    PLAYERS_EDIT = "players.edit"
    PLAYERS_CASH = "players.cash"
    REQUESTS_VIEW = "requests.view"
    REQUESTS_CREATE = "requests.create"
    REQUESTS_REVIEW = "requests.review"
    EVENTS_VIEW = "events.view"
    EVENTS_MANAGE = "events.manage"
    FIGHTS_VIEW = "fights.view"
    FIGHTS_RENAME = "fights.rename"
    FIGHTS_LOCK = "fights.lock"
    FIGHTS_DECLARE = "fights.declare"
    FIGHTS_REDECLARE = "fights.redeclare"
    FIGHTS_APPROVE = "fights.approve"
    BETS_VIEW = "bets.view"
    BETS_PLACE = "bets.place"
    BETS_CANCEL = "bets.cancel"
    COMMISSIONS_VIEW = "commissions.view"
    COMMISSIONS_CLAIM = "commissions.claim"
    CASHFLOW_VIEW = "cashflow.view"
    BOOKLETS_VIEW = "booklets.view"
    BOOKLETS_MANAGE = "booklets.manage"
    STATIONS_VIEW = "stations.view"
    STATIONS_MANAGE = "stations.manage"
    SETTINGS_VIEW = "settings.view"
    SETTINGS_MANAGE = "settings.manage"
    AUDIT_VIEW = "audit.view"
    TOKENS_MANAGE = "tokens.manage"
    BETS_CLAIM = "bets.claim"
    REPORTS_VIEW = "reports.view"
    CHAT_USE = "chat.use"


_AGENT_PERMS = {
    Perm.DASHBOARD, Perm.AGENTS_VIEW, Perm.AGENTS_EDIT, Perm.AGENTS_TRANSFER,
    Perm.PLAYERS_VIEW, Perm.PLAYERS_CREATE, Perm.PLAYERS_EDIT,
    Perm.REQUESTS_VIEW, Perm.REQUESTS_CREATE,
    Perm.EVENTS_VIEW, Perm.FIGHTS_VIEW, Perm.BETS_VIEW, Perm.BETS_PLACE, Perm.BETS_CANCEL,
    Perm.COMMISSIONS_VIEW, Perm.COMMISSIONS_CLAIM, Perm.CASHFLOW_VIEW, Perm.BOOKLETS_VIEW,
}

ROLE_PERMS: dict[Role, set[Perm]] = {
    Role.ADMIN: set(Perm),
    Role.MASTER: _AGENT_PERMS | {Perm.AGENTS_CREATE, Perm.STATIONS_VIEW},
    Role.GOLD: _AGENT_PERMS | {Perm.AGENTS_CREATE},
    Role.SUB: _AGENT_PERMS - {Perm.AGENTS_TRANSFER, Perm.AGENTS_EDIT} | {Perm.AGENTS_TRANSFER},
    Role.TELLER: {
        Perm.DASHBOARD, Perm.PLAYERS_VIEW, Perm.PLAYERS_CREATE, Perm.PLAYERS_CASH,
        Perm.EVENTS_VIEW, Perm.FIGHTS_VIEW, Perm.BETS_VIEW, Perm.BETS_PLACE, Perm.BETS_CANCEL,
        Perm.BOOKLETS_VIEW, Perm.STATIONS_VIEW, Perm.CASHFLOW_VIEW,
        Perm.BETS_CLAIM, Perm.CHAT_USE,
    },
    # Declarator: result entry only. No dashboard, bets, money or totals (hidden in the views too).
    Role.DECLARATOR: {Perm.EVENTS_VIEW, Perm.FIGHTS_VIEW, Perm.FIGHTS_LOCK, Perm.FIGHTS_DECLARE},
}
# Admin can also operate a counter when covering a shift.
ROLE_PERMS[Role.ADMIN] |= {Perm.PLAYERS_CASH}


def permissions_for(role: Role | str) -> set[Perm]:
    return ROLE_PERMS.get(Role(role), set())


def hides_totals(user: User) -> bool:
    """Declarators declare results without seeing stakes, pools or payouts."""
    return user.role == Role.DECLARATOR


def has_perm(user: User, perm: Perm) -> bool:
    if user.status != Status.ACTIVE:
        return False
    return perm in permissions_for(user.role)


def require_perm(user: User, perm: Perm) -> None:
    if not has_perm(user, perm):
        raise Forbidden("Your role is not allowed to perform this action.")


def can_create_role(parent: User, child_role: Role) -> bool:
    """Admin -> Master -> Gold -> Sub, exactly one level down. Admin may also
    create tellers and (for convenience) other admins."""
    if parent.status != Status.ACTIVE:
        return False
    if parent.role == Role.ADMIN:
        return child_role in (Role.ADMIN, Role.MASTER, Role.TELLER, Role.DECLARATOR)
    return CHILD_ROLE.get(Role(parent.role)) == child_role


def downline_ids(db: Session, user: User) -> set[int]:
    """All agent ids strictly below `user` (recursive). Admin -> everyone."""
    if user.role == Role.ADMIN:
        return set(db.scalars(select(User.id)).all())
    ids: set[int] = set()
    frontier = [user.id]
    while frontier:
        rows = db.scalars(select(User.id).where(User.parent_id.in_(frontier))).all()
        new = [r for r in rows if r not in ids]
        ids.update(new)
        frontier = new
    return ids


def scope_ids(db: Session, user: User) -> set[int]:
    """Downline plus self -- the set of agent ids whose data `user` may read."""
    return downline_ids(db, user) | {user.id}


def assert_user_in_scope(db: Session, actor: User, target_id: int) -> User:
    target = db.get(User, target_id)
    if target is None:
        raise Forbidden("Target account is not in your network.")
    if actor.role != Role.ADMIN and target_id not in scope_ids(db, actor):
        raise Forbidden("Target account is not in your network.")
    return target


def assert_player_in_scope(db: Session, actor: User, player: Player) -> Player:
    if actor.role == Role.ADMIN:
        return player
    if actor.role == Role.TELLER:
        # Counters serve tap-card holders only; online (GCash) accounts belong to an agent network.
        if player.kind != PlayerKind.CARD:
            raise Forbidden("Counter staff can only serve card players.")
        return player
    if player.agent_id is None or player.agent_id not in scope_ids(db, actor):
        raise Forbidden("That player is not in your network.")
    return player


def visible_agent_filter(actor: User):
    """SQL predicate helper for list pages (None = no restriction)."""
    if actor.role == Role.ADMIN:
        return None
    return User.parent_id == actor.id
