"""Server-side role & object permissions.

The original panel gated features by *hiding buttons* (`if($_SESSION[UTYPE]=='0')`)
while the AJAX handlers that actually moved money were unverified. In StelNexus
every route -- HTML or JSON -- passes through `require()`, and every object touch
passes through a downline scope check. UI hiding is cosmetic only.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import Forbidden
from app.core.settings_registry import GROUPS
from app.db.models import CHILD_ROLE, TOP_ROLES, Player, Role, Status, User


class Perm(str, Enum):
    DASHBOARD = "dashboard.view"
    AGENTS_VIEW = "agents.view"
    AGENTS_CREATE = "agents.create"
    AGENTS_EDIT = "agents.edit"
    PLAYERS_VIEW = "players.view"
    PLAYERS_CREATE = "players.create"
    PLAYERS_EDIT = "players.edit"
    REQUESTS_VIEW = "requests.view"
    REQUESTS_CREATE = "requests.create"
    REQUESTS_REVIEW = "requests.review"
    EVENTS_VIEW = "events.view"
    EVENTS_CREATE = "events.create"        # creating an event: Master and Admin only
    EVENTS_MANAGE = "events.manage"
    # Locations (venues). Master/Admin may register one while creating an event; editing,
    # archiving and deleting is the Admin Panel's job.
    LOCATIONS_CREATE = "locations.create"
    LOCATIONS_VIEW = "locations.view"
    LOCATIONS_MANAGE = "locations.manage"
    FIGHTS_VIEW = "fights.view"
    FIGHTS_RENAME = "fights.rename"
    FIGHTS_LOCK = "fights.lock"
    FIGHTS_DECLARE = "fights.declare"
    FIGHTS_REDECLARE = "fights.redeclare"
    FIGHTS_APPROVE = "fights.approve"
    BETS_VIEW = "bets.view"
    BETS_PLACE = "bets.place"
    BETS_CANCEL = "bets.cancel"
    # Bola: the two-number draw game (Tumbok-Sahod). Parallel to the sabong permissions.
    BOLA_VIEW = "bola.view"              # draws, results, own tickets
    BOLA_PLACE = "bola.place"            # place a bet for a player in your network
    BOLA_CANCEL = "bola.cancel"          # cancel a ticket inside the window
    BOLA_MANAGE = "bola.manage"          # create / open / lock / void draws, edit schedules
    BOLA_DECLARE = "bola.declare"        # enter the two balls
    BOLA_REDECLARE = "bola.redeclare"    # request a correction
    BOLA_APPROVE = "bola.approve"        # approve a correction (never your own)
    BOLA_EXPOSURE = "bola.exposure"      # stakes, exposure heat-map and draw totals
    COMMISSIONS_VIEW = "commissions.view"
    COMMISSIONS_CLAIM = "commissions.claim"
    CASHFLOW_VIEW = "cashflow.view"
    SETTINGS_VIEW = "settings.view"
    SETTINGS_MANAGE = "settings.manage"
    AUDIT_VIEW = "audit.view"
    TOKENS_MANAGE = "tokens.manage"
    REPORTS_VIEW = "reports.view"
    CHAT_USE = "chat.use"
    # Cut system: Master and Admin may change the Volume % / SOP % of the network.
    REFERRALS_MANAGE = "referrals.manage"
    CUTS_VIEW = "cuts.view"
    CUTS_MANAGE = "cuts.manage"
    # Operational emergency stop on withdrawals (the operator tier needs it; Settings is hidden).
    WITHDRAWALS_SWITCH = "withdrawals.switch"
    # The hidden layer: System/Hidden Over, house revenue lines, commission formula audit.
    HIDDEN_VIEW = "hidden.view"
    # The wallet hub (balance, load, cash out, transfer, money history): every account that holds money.
    WALLET_VIEW = "wallet.view"
    WALLET_TRANSFER = "wallet.transfer"
    # Admin Panel and what lives in it.
    PANEL_VIEW = "panel.view"
    PAWNS_MANAGE = "pawns.manage"          # only the Admin creates Pawns and sets what they may see
    ONLINE_VIEW = "online.view"            # who is signed in right now
    # Account tools in the Admin Panel. Admin always; a Pawn only through the matching grant.
    ACCOUNTS_VIEW = "accounts.view"        # the Accounts page (names, roles, status; no balances)
    ACCOUNTS_EDIT = "accounts.edit"        # change name / username / contact / GCash of an account
    ACCOUNTS_REMOVE = "accounts.remove"    # close an account (never a delete; see account_admin)
    IPBANS_MANAGE = "ipbans.manage"        # ban / unban IP addresses
    # Seller Desk (/sell) is experimental: Admin (the developer account) only, no grant lifts it.
    SELL_DESK = "sell.desk"


# ---------------------------------------------------------------------------
# Dev-only gate for the number game (see Settings.BOLA_DEV_ONLY)
# ---------------------------------------------------------------------------
BOLA_PERMS: frozenset[Perm] = frozenset(p for p in Perm if p.value.startswith("bola."))
BOLA_GROUP = "Bola"          # the Admin Panel variables group
BOLA_GRANT_SECTION = "Bola"  # the Pawn grants section


def bola_dev_only() -> bool:
    """True while the number game is visible to the developer (Admin) account only."""
    return bool(get_settings().BOLA_DEV_ONLY)


def bola_visible_to(user: User | None) -> bool:
    """May this account see that the number game exists at all (nav, cards, settings)?"""
    if user is None:
        return not bola_dev_only()
    return (not bola_dev_only()) or user.role == Role.ADMIN


# Everyone who holds money gets the wallet hub: load, cash out, transfer, and their own history.
_WALLET_PERMS = {Perm.WALLET_VIEW, Perm.WALLET_TRANSFER, Perm.REQUESTS_VIEW, Perm.REQUESTS_CREATE}

# Cavo / Kubrador: their own numbers, bets and referral codes. No Users page, no Players page.
_AGENT_PERMS = {
    Perm.DASHBOARD,
    Perm.EVENTS_VIEW, Perm.FIGHTS_VIEW, Perm.BETS_VIEW, Perm.BETS_PLACE, Perm.BETS_CANCEL,
    Perm.BOLA_VIEW, Perm.BOLA_PLACE, Perm.BOLA_CANCEL,
    Perm.COMMISSIONS_VIEW, Perm.COMMISSIONS_CLAIM,
} | _WALLET_PERMS

# Admin Panel territory: only the Admin has it by default, and a Pawn gets exactly the slices the
# Admin switches on (see GRANTS). Master does NOT have these.
_ADMIN_ONLY = {
    Perm.SETTINGS_VIEW, Perm.SETTINGS_MANAGE, Perm.AUDIT_VIEW, Perm.HIDDEN_VIEW,
    Perm.CUTS_VIEW, Perm.CUTS_MANAGE,
    Perm.PLAYERS_VIEW, Perm.PLAYERS_CREATE, Perm.PLAYERS_EDIT,
    Perm.ONLINE_VIEW, Perm.PANEL_VIEW, Perm.PAWNS_MANAGE,
    Perm.ACCOUNTS_VIEW, Perm.ACCOUNTS_EDIT, Perm.ACCOUNTS_REMOVE, Perm.IPBANS_MANAGE,
    Perm.SELL_DESK,
    Perm.LOCATIONS_VIEW, Perm.LOCATIONS_MANAGE,
    # House money is dev-only: the cashflow page, house totals on the dashboard, event P&L and the
    # report archives. A Pawn can still be granted them one by one ("cashflow:view", "reports:view").
    Perm.CASHFLOW_VIEW, Perm.REPORTS_VIEW,
    # Livestream chat (experimental) is for the Admin, and for a Pawn the Admin switched it on
    # for. Not Master.
    Perm.CHAT_USE,
}
# Master runs operations but never enters or changes results: declaring is for the Admin, Pawns
# and Declarators only (Master is a high-privilege operator, not a result source).
_NO_DECLARE = {Perm.FIGHTS_DECLARE, Perm.FIGHTS_REDECLARE, Perm.BOLA_DECLARE, Perm.BOLA_REDECLARE}
_OPERATOR = set(Perm) - _ADMIN_ONLY - _NO_DECLARE
# Kept for callers that ask "what is hidden from Master".
_HIDDEN_ONLY = _ADMIN_ONLY

# Declarator: result entry only. No balance, no bets, no money, no totals.
_DECLARATOR_PERMS = {Perm.EVENTS_VIEW, Perm.FIGHTS_VIEW, Perm.FIGHTS_LOCK, Perm.FIGHTS_DECLARE,
                     Perm.BOLA_VIEW, Perm.BOLA_DECLARE, Perm.BOLA_REDECLARE}

ROLE_PERMS: dict[Role, set[Perm]] = {
    Role.ADMIN: set(Perm),
    Role.MASTER: set(_OPERATOR),
    # Cavo and Kubrador only recruit through referral codes; the cut itself is not theirs to change.
    Role.CAVO: _AGENT_PERMS | {Perm.REFERRALS_MANAGE},
    Role.KUBRADOR: _AGENT_PERMS | {Perm.REFERRALS_MANAGE},
    # Retired role: tellers no longer exist. Old rows are closed by the migration and hold no permissions.
    Role.TELLER: set(),
    Role.DECLARATOR: set(_DECLARATOR_PERMS),
    # Pawn: a declarator-style spectator. Everything beyond that is granted per account by the Admin.
    Role.PAWN: set(_DECLARATOR_PERMS),
}


# ---------------------------------------------------------------------------
# Pawn grants: what the Admin may switch on for each Pawn
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Grant:
    key: str
    label: str
    help: str
    section: str
    perms: frozenset
    implies: tuple = ()


def _build_grants() -> tuple[Grant, ...]:
    out: list[Grant] = []
    for group in GROUPS:
        out.append(Grant(f"vars:{group}:view", f"See {group} variables", "", "Variables",
                         frozenset({Perm.PANEL_VIEW, Perm.SETTINGS_VIEW})))
        out.append(Grant(f"vars:{group}:edit", f"Change {group} variables", "", "Variables",
                         frozenset({Perm.PANEL_VIEW, Perm.SETTINGS_VIEW, Perm.SETTINGS_MANAGE}),
                         implies=(f"vars:{group}:view",)))
    out += [
        Grant("locations:view", "See locations", "", "Locations",
              frozenset({Perm.PANEL_VIEW, Perm.LOCATIONS_VIEW})),
        Grant("locations:edit", "Edit, archive and delete locations", "", "Locations",
              frozenset({Perm.PANEL_VIEW, Perm.LOCATIONS_VIEW, Perm.LOCATIONS_MANAGE, Perm.LOCATIONS_CREATE}),
              implies=("locations:view",)),
        Grant("cuts:view", "See cuts", "Volume % and SOP % of every Cavo and Kubrador.", "Cuts",
              frozenset({Perm.PANEL_VIEW, Perm.CUTS_VIEW})),
        Grant("cuts:edit", "Change cuts", "Held to the same limits as Settings → Commissions.", "Cuts",
              frozenset({Perm.PANEL_VIEW, Perm.CUTS_VIEW, Perm.CUTS_MANAGE}), implies=("cuts:view",)),
        Grant("players:view", "See the Players page", "", "People",
              frozenset({Perm.PLAYERS_VIEW})),
        Grant("players:edit", "Register and edit players", "", "People",
              frozenset({Perm.PLAYERS_VIEW, Perm.PLAYERS_CREATE, Perm.PLAYERS_EDIT}), implies=("players:view",)),
        Grant("agents:create", "Create Cavo and Kubrador accounts",
              "Directly, with no referral code. Picks the upline. Sees the Users list without balances.",
              "People", frozenset({Perm.AGENTS_VIEW, Perm.AGENTS_CREATE})),
        Grant("online:view", "See who is online", "Live list of signed-in accounts.", "People",
              frozenset({Perm.ONLINE_VIEW})),
        Grant("accounts:edit", "Edit account details",
              "Name, username, contact and GCash number of any Master, Cavo, Kubrador, Declarator or player. "
              "Never the Admin or another Pawn.", "People",
              frozenset({Perm.PANEL_VIEW, Perm.ACCOUNTS_VIEW, Perm.ACCOUNTS_EDIT})),
        Grant("accounts:remove", "Remove accounts",
              "Closes the account and signs it out. Refused while it holds credit, has open bets or a "
              "downline. Never the Admin or another Pawn.", "People",
              frozenset({Perm.PANEL_VIEW, Perm.ACCOUNTS_VIEW, Perm.ACCOUNTS_REMOVE})),
        Grant("ipbans:manage", "Ban and unban IP addresses",
              "Blocks an address or small range from the whole site. Cannot ban its own address or the Admin's.",
              "Security", frozenset({Perm.PANEL_VIEW, Perm.IPBANS_MANAGE})),
        Grant("audit:view", "See the audit trail and ledger integrity", "", "Reports",
              frozenset({Perm.PANEL_VIEW, Perm.AUDIT_VIEW})),
        Grant("cashflow:view", "See cashflow", "House position and money totals.", "Reports",
              frozenset({Perm.CASHFLOW_VIEW})),
        Grant("reports:view", "See report archives", "", "Reports",
              frozenset({Perm.REPORTS_VIEW})),
        Grant("totals:view", "See stakes, pools and payouts", "Declarators never see these.", "Reports",
              frozenset()),
        Grant("bola:view", "See the number game (Bola)", "Draws, schedules and results.", "Bola",
              frozenset({Perm.BOLA_VIEW})),
        Grant("bola:declare", "Declare number-game results",
              "Enter the two balls and request a correction. Never approves their own.", "Bola",
              frozenset({Perm.BOLA_VIEW, Perm.BOLA_DECLARE, Perm.BOLA_REDECLARE}),
              implies=("bola:view",)),
        Grant("bola:totals", "See number-game stakes and exposure",
              "Implies 'See stakes, pools and payouts', so the account stops being totals-blind.",
              "Bola", frozenset({Perm.BOLA_EXPOSURE}), implies=("bola:view", "totals:view")),
        Grant("chat", "Use the livestream chat (experimental)",
              "The live operations room: messages, who is in the room and what is on air.", "Livestream",
              frozenset({Perm.CHAT_USE})),
    ]
    return tuple(out)


GRANTS: tuple[Grant, ...] = _build_grants()
GRANT_BY_KEY: dict[str, Grant] = {g.key: g for g in GRANTS}


def normalize_grants(keys) -> list[str]:
    """Drop unknown keys and add what each key implies (edit => view). Order is stable."""
    wanted = set()
    for key in keys or []:
        grant = GRANT_BY_KEY.get(str(key))
        if grant is None:
            continue
        wanted.add(grant.key)
        wanted.update(k for k in grant.implies if k in GRANT_BY_KEY)
    return [g.key for g in GRANTS if g.key in wanted]


def permissions_for(role: Role | str) -> set[Perm]:
    """The role's base permissions (a Pawn's per-account grants are added by permissions_of)."""
    role = Role(role)
    perms = set(ROLE_PERMS.get(role, set()))
    if bola_dev_only() and role != Role.ADMIN:
        perms -= BOLA_PERMS
    return perms


def permissions_of(user: User) -> set[Perm]:
    """Everything this account may do: its role plus, for a Pawn, the grants the Admin switched on."""
    perms = permissions_for(user.role)
    for key in user.grant_set:
        grant = GRANT_BY_KEY.get(key)
        if grant is not None:
            perms |= set(grant.perms)
    if bola_dev_only() and user.role != Role.ADMIN:
        perms -= BOLA_PERMS   # no role or Pawn grant can lift the gate
    return perms


def _groups_with(user: User, suffix: str) -> set[str]:
    if user.role == Role.ADMIN:
        return set(GROUPS)
    if user.role == Role.PAWN:
        grants = user.grant_set
        found = {g for g in GROUPS if f"vars:{g}:{suffix}" in grants}
        if bola_dev_only():
            found.discard(BOLA_GROUP)
        return found
    return set()


def offered_grants() -> tuple[Grant, ...]:
    """The grants the Admin can tick for a Pawn. Bola grants are hidden while Bola is dev-only."""
    if not bola_dev_only():
        return GRANTS
    return tuple(g for g in GRANTS
                 if g.section != BOLA_GRANT_SECTION and not g.key.startswith(f"vars:{BOLA_GROUP}:"))


def variable_groups_visible(user: User) -> set[str]:
    """Settings groups this account may read in the Admin Panel."""
    return _groups_with(user, "view") | _groups_with(user, "edit")


def variable_groups_editable(user: User) -> set[str]:
    return _groups_with(user, "edit")




def hides_totals(user: User) -> bool:
    """Declarators declare results without seeing stakes, pools or payouts. A Pawn is the same
    until the Admin switches on 'totals:view'."""
    if user.role == Role.DECLARATOR:
        return True
    return user.role == Role.PAWN and "totals:view" not in user.grant_set


def has_perm(user: User, perm: Perm) -> bool:
    if user.status != Status.ACTIVE:
        return False
    return perm in permissions_of(user)


def require_perm(user: User, perm: Perm) -> None:
    if not has_perm(user, perm):
        raise Forbidden("Your role is not allowed to perform this action.")


def is_top(user: User) -> bool:
    """Admin or Master: sees the whole visible network and runs operations."""
    return user.role in TOP_ROLES


def has_global_scope(user: User) -> bool:
    """Who may look across the whole visible network: the operator tier, plus Pawns (read-only
    spectators whose reach is limited by their grants, not by the tree)."""
    return user.role in TOP_ROLES or user.role == Role.PAWN


def can_create_role(parent: User, child_role: Role) -> bool:
    """Who may create which account directly (no referral code).

    Admin -> Master, Cavo, Kubrador, Declarator, Pawn (Cavo/Kubrador under an upline the Admin picks).
    Pawn  -> Cavo, Kubrador, only when the Admin ticked "Create Cavo and Kubrador accounts".
    Master -> Cavo, Declarator.  Cavo -> Kubrador (through its referral code in practice).
    There is only ever ONE Admin and only the Admin can create a Pawn."""
    if parent.status != Status.ACTIVE:
        return False
    if parent.role == Role.ADMIN:
        return child_role in (Role.MASTER, Role.CAVO, Role.KUBRADOR, Role.DECLARATOR, Role.PAWN)
    if parent.role == Role.PAWN:
        return child_role in (Role.CAVO, Role.KUBRADOR) and Perm.AGENTS_CREATE in permissions_of(parent)
    if parent.role == Role.MASTER:
        return child_role in (Role.CAVO, Role.DECLARATOR)
    return CHILD_ROLE.get(Role(parent.role)) == child_role


def creatable_roles(user: User) -> list[Role]:
    """The user types offered on the Create user form (Pawns are made in the Admin Panel)."""
    return [r for r in (Role.MASTER, Role.CAVO, Role.KUBRADOR, Role.DECLARATOR) if can_create_role(user, r)]


def _visible_ids(db: Session, user: User) -> set[int]:
    """Every id the operator tier may see: Admin sees all, Master sees all but hidden accounts."""
    stmt = select(User.id)
    if user.role != Role.ADMIN:
        stmt = stmt.where(User.role != Role.ADMIN)
    return set(db.scalars(stmt).all())


def downline_ids(db: Session, user: User) -> set[int]:
    """All account ids strictly below `user` (recursive). Admin/Master/Pawn -> everyone they may see."""
    if has_global_scope(user):
        return _visible_ids(db, user) - {user.id}
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
    # A hidden account answers exactly like a missing one, so its existence cannot be probed.
    if target is None or (target.is_hidden and actor.role != Role.ADMIN):
        raise Forbidden("Target account is not in your network.")
    if not has_global_scope(actor) and target_id not in scope_ids(db, actor):
        raise Forbidden("Target account is not in your network.")
    return target


def assert_player_in_scope(db: Session, actor: User, player: Player) -> Player:
    if has_global_scope(actor):
        return player
    if player.agent_id is None or player.agent_id not in scope_ids(db, actor):
        raise Forbidden("That player is not in your network.")
    return player


def visible_agent_filter(actor: User):
    """SQL predicate helper for list pages (None = no restriction)."""
    if actor.role == Role.ADMIN:
        return None
    if actor.role in (Role.MASTER, Role.PAWN):
        return User.role != Role.ADMIN
    return User.parent_id == actor.id
