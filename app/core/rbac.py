"""Server-side role & object permissions.

The original panel gated features by *hiding buttons* (`if($_SESSION[UTYPE]=='0')`)
while the AJAX handlers that actually moved money were unverified. In StelNexus
every route -- HTML or JSON -- passes through `require()`, and every object touch
passes through a downline scope check. UI hiding is cosmetic only.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import Forbidden
from app.core.settings_registry import GROUPS
from app.core import rules as rules_mod
from app.db.models import ADMIN_TIER, CHILD_ROLES, TOP_ROLES, Player, Role, Status, User


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
    EVENTS_CREATE = "events.create"        # creating an event: Master, Agent, Sub Agent and Admin (hosting)
    EVENTS_MANAGE = "events.manage"
    EVENTS_OPEN = "events.open"            # open or close an event (Declarators); drafting / cancelling stays EVENTS_MANAGE
    # Locations (venues). Master/Admin may register one while creating an event; editing,
    # archiving and deleting is the Admin Panel's job.
    LOCATIONS_CREATE = "locations.create"
    LOCATIONS_VIEW = "locations.view"
    LOCATIONS_MANAGE = "locations.manage"
    # Sports team catalog (NBA first): registry only, Admin Panel.
    TEAMS_VIEW = "teams.view"
    TEAMS_MANAGE = "teams.manage"
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
    BOLA_OPEN = "bola.open"              # open / close betting on a draw (Declarators); no create, void or schedules
    BOLA_DECLARE = "bola.declare"        # enter the two balls
    BOLA_REDECLARE = "bola.redeclare"    # request a correction
    BOLA_APPROVE = "bola.approve"        # approve a correction (never your own)
    BOLA_EXPOSURE = "bola.exposure"      # stakes, exposure heat-map and draw totals
    BOLA_SKIP = "bola.skip"              # skip the wait: close betting and bring the draw time to now (dev-only by default)
    # Scatter: the 6x5 scatter-pays slot. Played from the account's own credit; there is nothing to manage per round.
    SCATTER_VIEW = "scatter.view"        # open the game, see own rounds
    SCATTER_PLAY = "scatter.play"        # spin with own credit
    SCATTER_REPORT = "scatter.report"    # stakes, wins and the house result across all players
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
    PAWNS_MANAGE = "pawns.manage"          # the Access manager: create Pawns, set what any account below may do
    BISHOPS_MANAGE = "bishops.manage"      # KING ONLY: appoint, edit and remove Bishops
    KING_DESK = "king.desk"                # KING ONLY: emergency switches, what is sealed from Bishops, approval policy
    APPROVALS_DECIDE = "approvals.decide"  # answer other people's requests for a right (King and Bishops)
    ONLINE_VIEW = "online.view"            # who is signed in right now
    # Account tools in the Admin Panel. Admin always; a Pawn only through the matching grant.
    ACCOUNTS_VIEW = "accounts.view"        # the Accounts page (names, roles, status; no balances)
    ACCOUNTS_EDIT = "accounts.edit"        # change name / username / contact / GCash of an account
    ACCOUNTS_REMOVE = "accounts.remove"    # close an account (never a delete; see account_admin)
    IPBANS_MANAGE = "ipbans.manage"        # ban / unban IP addresses
    STREAMS_VIEW = "streams.view"          # Staff Panel -> Stream: see the video sources
    STREAMS_MANAGE = "streams.manage"      # add / take live / stop video sources
    # Cancel Ticket and Bola Cancelled pages: dev-only (Admin account).
    CANCEL_LOG = "cancellog.view"
    # Staff Panel (below the Admin Panel): one section per staff role. Each section opens only for
    # its own role, the Admin, and a Pawn the Admin switched it on for. Master never.
    STAFF_VIEW = "staff.view"
    STAFF_TELLER = "staff.teller"
    STAFF_DECLARATOR = "staff.declarator"
    # The system pool: the house reserve (a share of every game's cut) that builds up on the Teller page. Locked: it only
    # ever fills from settlement. Admin, Bishop and Tellers see it; a Pawn only if the Admin granted it.
    SYSTEM_POOL_VIEW = "systempool.view"
    # Agent Panel (below the Staff Panel): the Agent's own desk. Agents hold it; Master, Sub Agents and players never;
    # the dev team (Admin, Bishop) can open it too. AGENT_ROOM_CUTS = set what the Agent's Sub Agents earn, per room.
    AGENT_PANEL_VIEW = "agentpanel.view"
    AGENT_ROOM_CUTS = "agentpanel.cuts"


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

# Agent / Sub Agent: their own numbers, bets, commission statements and referral codes. No Users
# page, no Players page.
_AGENT_PERMS = {
    Perm.DASHBOARD,
    Perm.EVENTS_VIEW, Perm.FIGHTS_VIEW, Perm.BETS_VIEW, Perm.BETS_PLACE,   # no BETS_CANCEL: undoing a bet is dev-only
    Perm.BOLA_VIEW, Perm.BOLA_PLACE,        # no BOLA_CANCEL: undoing a number-game bet is dev-only too
    Perm.SCATTER_VIEW, Perm.SCATTER_PLAY,
    Perm.COMMISSIONS_VIEW, Perm.COMMISSIONS_CLAIM,
    # Hosting: an Agent / Sub Agent may create an event (and register its venue). Running it (lock, odds,
    # cancel, expenses) stays with Master / Admin: EVENTS_MANAGE is not scoped to the event's creator.
    Perm.EVENTS_CREATE, Perm.LOCATIONS_CREATE,
} | _WALLET_PERMS

# Admin Panel territory: only the Admin has it by default, and a Pawn gets exactly the slices the
# Admin switches on (see GRANTS). Master does NOT have these.
_ADMIN_ONLY = {
    Perm.SETTINGS_VIEW, Perm.SETTINGS_MANAGE, Perm.AUDIT_VIEW, Perm.HIDDEN_VIEW,
    Perm.CUTS_VIEW, Perm.CUTS_MANAGE,
    Perm.PLAYERS_VIEW, Perm.PLAYERS_CREATE, Perm.PLAYERS_EDIT,
    Perm.ONLINE_VIEW, Perm.PANEL_VIEW, Perm.PAWNS_MANAGE, Perm.BISHOPS_MANAGE, Perm.KING_DESK, Perm.APPROVALS_DECIDE,
    Perm.ACCOUNTS_VIEW, Perm.ACCOUNTS_EDIT, Perm.ACCOUNTS_REMOVE, Perm.IPBANS_MANAGE,
    Perm.STREAMS_VIEW, Perm.STREAMS_MANAGE,
    Perm.CANCEL_LOG,
    # Undoing / cancelling a placed bet (livestream Undo, ticket page, API) is dev-only: once a bet is in, players,
    # agents and Master cannot take it back. Only the Admin (or a Pawn it was explicitly granted to) can.
    Perm.BETS_CANCEL, Perm.BOLA_CANCEL,
    # Skipping a draw's wait (the cut-off countdown) is dev-only too; the Admin can allow it on any account.
    Perm.BOLA_SKIP,
    Perm.STAFF_VIEW, Perm.STAFF_TELLER, Perm.STAFF_DECLARATOR,
    # The Agent Panel is for Agents (they get it below) and the dev team; Master gets it view-only in ROLE_PERMS.
    Perm.AGENT_PANEL_VIEW, Perm.AGENT_ROOM_CUTS,
    Perm.LOCATIONS_VIEW, Perm.LOCATIONS_MANAGE, Perm.TEAMS_VIEW, Perm.TEAMS_MANAGE,
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
# Approving / rejecting is for designated staff, not for Master: load and cash-out requests belong to the
# Teller desk (and the Admin), result corrections to the Admin. Master still sees its own requests.
_NO_APPROVE = {Perm.REQUESTS_REVIEW, Perm.FIGHTS_APPROVE, Perm.BOLA_APPROVE}
_OPERATOR = set(Perm) - _ADMIN_ONLY - _NO_DECLARE - _NO_APPROVE
# Kept for callers that ask "what is hidden from Master".
_HIDDEN_ONLY = _ADMIN_ONLY

# Declarator: result entry only. No balance, no bets, no money, no totals.
_DECLARATOR_PERMS = {Perm.EVENTS_VIEW, Perm.FIGHTS_VIEW, Perm.FIGHTS_LOCK, Perm.FIGHTS_DECLARE,
                     Perm.BOLA_VIEW, Perm.BOLA_DECLARE, Perm.BOLA_REDECLARE}

# Teller: the wallet desk and nothing else (approve / reject requests, see Users). No balance.
# The Staff Panel's People page shows a Teller only who is online and their GCash numbers (see routes_people).
_TELLER_PERMS = {Perm.WALLET_VIEW, Perm.REQUESTS_VIEW, Perm.REQUESTS_REVIEW, Perm.AGENTS_VIEW,
                 Perm.STAFF_VIEW, Perm.STAFF_TELLER, Perm.ONLINE_VIEW, Perm.SYSTEM_POOL_VIEW}

# What only the King may ever hold: nobody below it can be given these, whatever is ticked anywhere.
KING_ONLY: frozenset[Perm] = frozenset({Perm.BISHOPS_MANAGE, Perm.KING_DESK})
# Page-access and look-only rights. A frozen account keeps exactly these; "ask first" is meaningless for them.
VIEW_PERMS: frozenset[Perm] = frozenset(
    {p for p in Perm if p.value.endswith(".view")} | {Perm.STAFF_TELLER, Perm.STAFF_DECLARATOR})

ROLE_PERMS: dict[Role, set[Perm]] = {
    Role.ADMIN: set(Perm),
    # The Bishop: every right except the King's own. What the King seals on top comes off live (see sealed_perms).
    Role.BISHOP: set(Perm) - KING_ONLY,
    # Master opens the Agent Panel, sets what its own Agents and their Sub Agents earn per room, and approves Sub Agent
    # panel requests there. It does not get the Admin's Cuts page (room defaults stay with the Admin side).
    Role.MASTER: set(_OPERATOR) | {Perm.AGENT_PANEL_VIEW, Perm.AGENT_ROOM_CUTS},
    # Agent and Sub Agent recruit through referral codes. Their own Volume % is not theirs to change.
    # An Agent also gets the Agent Panel and may set what its Sub Agents earn per room; a Sub Agent has no one below it.
    Role.AGENT: _AGENT_PERMS | {Perm.REFERRALS_MANAGE, Perm.AGENT_PANEL_VIEW, Perm.AGENT_ROOM_CUTS},
    Role.SUB_AGENT: _AGENT_PERMS | {Perm.REFERRALS_MANAGE},
    # Retired tiers (merged into Agent / Sub Agent by the migration): no permissions.
    Role.TELLER: set(_TELLER_PERMS),
    Role.DECLARATOR: set(_DECLARATOR_PERMS) | {Perm.EVENTS_OPEN, Perm.BOLA_OPEN, Perm.STAFF_VIEW, Perm.STAFF_DECLARATOR,
                                                 Perm.STREAMS_VIEW, Perm.STREAMS_MANAGE},   # the stream is the Declarator's job
    # Pawn: a declarator-style spectator. Everything beyond that is granted per account by the Admin.
    Role.PAWN: set(_DECLARATOR_PERMS),
}

# Player (a normal account recruited with a Sub Agent's referral code): plays and nothing else. Strictly below
# a Sub Agent: no dashboard, no commission, no referral codes, no hosting, no transfers, no cancelling.
# It can see the games, place its own bets, see its own bets and load / cash out its own credit.
PLAYER_PERMS: frozenset[Perm] = frozenset({
    Perm.EVENTS_VIEW, Perm.FIGHTS_VIEW, Perm.BETS_VIEW, Perm.BETS_PLACE,
    Perm.BOLA_VIEW, Perm.BOLA_PLACE,
    Perm.SCATTER_VIEW, Perm.SCATTER_PLAY,
    Perm.WALLET_VIEW, Perm.REQUESTS_VIEW, Perm.REQUESTS_CREATE,
})


def builtin_player_perms() -> set[Perm]:
    """The Player rights written in code, before any change from the Admin."""
    return set(PLAYER_PERMS)


# ---------------------------------------------------------------------------
# The engine: role defaults -> Pawn grants -> account type -> the account's own settings -> passes
# Everything the King or a Bishop sets lives in the database and is read through a short cache, so a change
# applies within seconds with no restart (and with no deploy).
# ---------------------------------------------------------------------------
log = logging.getLogger("stelnexus.rbac")

PLAYER_ROLE_KEY = "player"
_ROLE_CACHE_TTL = 5.0            # seconds; other workers pick a change up within this time


@dataclass
class Layer:
    """One level of settings: rights switched on, rights switched off, and conditional rules."""
    grants: frozenset = frozenset()
    revokes: frozenset = frozenset()
    rules: dict = field(default_factory=dict)        # Perm -> clean rule (app.core.rules)

    def is_empty(self) -> bool:
        return not (self.grants or self.revokes or self.rules)


EMPTY_LAYER = Layer()


@dataclass
class Snapshot:
    roles: dict      # role key -> Layer
    types: dict      # account profile id -> Layer
    flags: dict      # King switches (access_flags)
    passes: dict     # user id -> {Perm: datetime}: approved, time-boxed passes still running


_snapshot_cache: tuple[float, Snapshot] | None = None
_snapshot_lock = threading.Lock()

FLAG_DEFAULTS: dict[str, object] = {
    "freeze_pawns": False,            # Pawns keep only look-only rights
    "freeze_bishops": False,          # Bishops keep only look-only rights
    "approvals_paused": False,        # "ask first" counts as No while this is on, and no new passes work
    "sealed_perms": [Perm.HIDDEN_VIEW.value],   # rights kept from the Dev tree (Bishops and Pawns) on top of KING_ONLY
    "sealed_groups": ["Security"],    # Admin Panel variable groups kept from the Dev tree
    "request_ttl_hours": 24,          # an unanswered request lapses after this long
    "bishops_may_approve": True,      # off: only the King answers requests
    "max_pass_hours": 168,            # the longest "for a while" pass anyone may hand out
}


def _parse_perm_keys(raw) -> set[Perm]:
    """Known, delegable permission keys only (grants). NON_DELEGABLE and unknown keys are dropped, so even a tampered
    row can never hand out the keys to the Access manager."""
    out: set[Perm] = set()
    for key in (raw or []):
        try:
            perm = Perm(str(key))
        except ValueError:
            continue
        if perm not in NON_DELEGABLE:
            out.add(perm)
    return out


def _parse_revoke_keys(raw) -> set[Perm]:
    """Revokes may name any known right: the King can take the Access manager away from a Bishop."""
    out: set[Perm] = set()
    for key in (raw or []):
        try:
            out.add(Perm(str(key)))
        except ValueError:
            continue
    return out


def parse_layer(grants, revokes, rules) -> Layer:
    """A trustworthy Layer from stored JSON. Malformed rules are dropped; "ask" on a look-only right is dropped."""
    clean: dict = {}
    for key, raw in (rules or {}).items():
        try:
            perm = Perm(str(key))
        except ValueError:
            continue
        rule = rules_mod.clean_rule(raw)
        if rule is None or perm in NON_DELEGABLE or perm in KING_ONLY:
            continue
        if rule["mode"] == "ask" and perm in VIEW_PERMS:
            continue
        clean[perm] = rule
    return Layer(frozenset(_parse_perm_keys(grants)), frozenset(_parse_revoke_keys(revokes)), clean)


def _load_snapshot() -> Snapshot:
    from app.db.models import AccessFlag, AccessRequest, AccountProfile, RolePermOverride
    from app.db.session import SessionLocal
    session = SessionLocal()
    try:
        roles = {r.role: parse_layer(r.perm_grants, r.perm_revokes, r.perm_rules)
                 for r in session.scalars(select(RolePermOverride)).all()}
        types = {t.id: parse_layer(t.perm_grants, t.perm_revokes, t.perm_rules)
                 for t in session.scalars(select(AccountProfile)).all()}
        flags = {f.key: f.value for f in session.scalars(select(AccessFlag)).all()}
        now = rules_mod.utcnow()
        passes: dict = {}
        for row in session.scalars(select(AccessRequest).where(
                AccessRequest.status == "approved", AccessRequest.pass_until.is_not(None),
                AccessRequest.uses_left.is_(None), AccessRequest.bind_hash.is_(None))).all():
            until = row.pass_until if row.pass_until.tzinfo else row.pass_until.replace(tzinfo=dt.timezone.utc)
            if until <= now:
                continue
            try:
                perm = Perm(row.perm)
            except ValueError:
                continue
            slot = passes.setdefault(row.requester_id, {})
            slot[perm] = max(until, slot.get(perm, until))
        return Snapshot(roles, types, flags, passes)
    finally:
        session.close()


def snapshot() -> Snapshot:
    """The settings the King and Bishops made, cached for a few seconds. On a database error the last good copy
    (or the built-in defaults) applies, logged, so a hiccup never locks anybody out or opens anything up."""
    global _snapshot_cache
    cached = _snapshot_cache
    if cached is not None and time.monotonic() - cached[0] < _ROLE_CACHE_TTL:
        return cached[1]
    with _snapshot_lock:
        cached = _snapshot_cache
        if cached is not None and time.monotonic() - cached[0] < _ROLE_CACHE_TTL:
            return cached[1]
        try:
            data = _load_snapshot()
        except Exception:                                    # noqa: BLE001 - never break authorization on a read error
            log.exception("Could not read access settings; using the last good copy or the built-in defaults")
            data = cached[1] if cached is not None else Snapshot({}, {}, {}, {})
        _snapshot_cache = (time.monotonic(), data)
        return data


def invalidate_role_overrides() -> None:
    global _snapshot_cache
    with _snapshot_lock:
        _snapshot_cache = None


def role_overrides() -> dict:
    """{role key: Layer} the Admin set on role defaults (a role with no row has the built-in defaults)."""
    return snapshot().roles


def type_overrides() -> dict:
    return snapshot().types


def flag(name: str):
    return snapshot().flags.get(name, FLAG_DEFAULTS[name])


def sealed_perms() -> set[Perm]:
    """Rights the King keeps from everyone in the Dev tree (Bishops and Pawns). KING_ONLY is always included."""
    out = set(KING_ONLY)
    for key in (flag("sealed_perms") or []):
        try:
            out.add(Perm(str(key)))
        except ValueError:
            continue
    return out


def sealed_groups() -> set[str]:
    return {str(g) for g in (flag("sealed_groups") or [])}


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
        Grant("teams:view", "See the sports teams", "NBA team registry.", "Teams",
              frozenset({Perm.PANEL_VIEW, Perm.TEAMS_VIEW})),
        Grant("teams:edit", "Switch teams on or off", "", "Teams",
              frozenset({Perm.PANEL_VIEW, Perm.TEAMS_VIEW, Perm.TEAMS_MANAGE}), implies=("teams:view",)),
        Grant("cuts:view", "See cuts", "Volume % and SOP % of every Agent and Sub Agent.", "Cuts",
              frozenset({Perm.PANEL_VIEW, Perm.CUTS_VIEW})),
        Grant("cuts:edit", "Change cuts", "Held to the same limits as Settings → Commissions.", "Cuts",
              frozenset({Perm.PANEL_VIEW, Perm.CUTS_VIEW, Perm.CUTS_MANAGE}), implies=("cuts:view",)),
        Grant("players:view", "See the Players page", "", "People",
              frozenset({Perm.PLAYERS_VIEW})),
        Grant("players:edit", "Register and edit players", "", "People",
              frozenset({Perm.PLAYERS_VIEW, Perm.PLAYERS_CREATE, Perm.PLAYERS_EDIT}), implies=("players:view",)),
        Grant("agents:create", "Create Agent and Sub Agent accounts",
              "Directly, with no referral code. Picks the upline. Sees the Users list without balances.",
              "People", frozenset({Perm.AGENTS_VIEW, Perm.AGENTS_CREATE})),
        Grant("online:view", "See who is online", "Live list of signed-in accounts.", "People",
              frozenset({Perm.ONLINE_VIEW})),
        Grant("accounts:edit", "Edit account details",
              "Name, username, contact and GCash number of any Master Agent, Agent, Sub Agent, Declarator or player. "
              "Never the Admin or another Pawn.", "People",
              frozenset({Perm.PANEL_VIEW, Perm.ACCOUNTS_VIEW, Perm.ACCOUNTS_EDIT})),
        Grant("accounts:remove", "Remove accounts",
              "Closes the account and signs it out. Refused while it holds credit, has open bets or a "
              "downline. Never the Admin or another Pawn.", "People",
              frozenset({Perm.PANEL_VIEW, Perm.ACCOUNTS_VIEW, Perm.ACCOUNTS_REMOVE})),
        Grant("ipbans:manage", "Ban and unban IP addresses",
              "Blocks an address or small range from the whole site. Cannot ban its own address or the Admin's.",
              "Security", frozenset({Perm.PANEL_VIEW, Perm.IPBANS_MANAGE})),
        Grant("streams:manage", "Manage the livestream and videos",
              "Paste a stream / video link, preview it, take it live for everyone or stop it.", "Security",
              frozenset({Perm.STAFF_VIEW, Perm.STREAMS_VIEW, Perm.STREAMS_MANAGE})),
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
        Grant("bola:open", "Open and close number-game betting",
              "Open or close betting on a draw. Cannot create, void or reschedule draws.", "Bola",
              frozenset({Perm.BOLA_VIEW, Perm.BOLA_OPEN}), implies=("bola:view",)),
        Grant("bola:totals", "See number-game stakes and exposure",
              "Implies 'See stakes, pools and payouts', so the account stops being totals-blind.",
              "Bola", frozenset({Perm.BOLA_EXPOSURE}), implies=("bola:view", "totals:view")),
        Grant("staff:teller", "See the Staff Panel \u2192 Teller section",
              "Read-only view of every GCash number, its limits and today's usage. Cannot add or change numbers.",
              "Staff Panel", frozenset({Perm.STAFF_VIEW, Perm.STAFF_TELLER})),
        Grant("systempool:view", "See the system pool on the Teller page",
              "Read-only: the house reserve that builds up from every game's cut. Nobody can move it from there.",
              "Staff Panel", frozenset({Perm.STAFF_VIEW, Perm.STAFF_TELLER, Perm.SYSTEM_POOL_VIEW})),
        Grant("staff:declarator", "See the Staff Panel \u2192 Declarator section", "",
              "Staff Panel", frozenset({Perm.STAFF_VIEW, Perm.STAFF_DECLARATOR})),
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


# Never delegable and never removable by an override: the keys to this very manager. Only the Admin holds it
# (the Admin's own rights are not editable either), so nobody can grant themselves or a friend the power to
# change permissions.
NON_DELEGABLE: frozenset[Perm] = frozenset({Perm.PAWNS_MANAGE, Perm.BISHOPS_MANAGE, Perm.KING_DESK, Perm.APPROVALS_DECIDE})

# (category, ((perm, plain-language label), ...)) -- drives the rights chart and the per-account editor.
PERM_CATALOG: tuple[tuple[str, tuple[tuple[Perm, str], ...]], ...] = (
    ("Dashboard & people", (
        (Perm.DASHBOARD, "See the dashboard"), (Perm.AGENTS_VIEW, "See the Users (agents) list"),
        (Perm.AGENTS_CREATE, "Create Agent / Sub Agent accounts"), (Perm.AGENTS_EDIT, "Edit agents"),
        (Perm.PLAYERS_VIEW, "See the Players page"), (Perm.PLAYERS_CREATE, "Register players"),
        (Perm.PLAYERS_EDIT, "Edit players"), (Perm.ONLINE_VIEW, "See who is online"))),
    ("Wallet & requests", (
        (Perm.WALLET_VIEW, "Wallet hub (balance, history)"), (Perm.WALLET_TRANSFER, "Transfer credit"),
        (Perm.REQUESTS_VIEW, "See load / cash-out requests"), (Perm.REQUESTS_CREATE, "Create load / cash-out requests"),
        (Perm.REQUESTS_REVIEW, "Approve / reject requests (Teller desk)"),
        (Perm.WITHDRAWALS_SWITCH, "Emergency stop on withdrawals"))),
    ("Events, venues & teams", (
        (Perm.EVENTS_VIEW, "See events"), (Perm.EVENTS_CREATE, "Create / host an event"),
        (Perm.EVENTS_MANAGE, "Run events (draft, odds, cancel, expenses)"), (Perm.EVENTS_OPEN, "Open / close an event"),
        (Perm.LOCATIONS_CREATE, "Register a venue"), (Perm.LOCATIONS_VIEW, "See locations"),
        (Perm.LOCATIONS_MANAGE, "Edit / archive / delete locations"), (Perm.TEAMS_VIEW, "See sports team catalog"),
        (Perm.TEAMS_MANAGE, "Switch teams on / off"))),
    ("Fights & bets (sabong)", (
        (Perm.FIGHTS_VIEW, "See fights"), (Perm.FIGHTS_RENAME, "Rename fights"), (Perm.FIGHTS_LOCK, "Lock fights"),
        (Perm.FIGHTS_DECLARE, "Declare results"), (Perm.FIGHTS_REDECLARE, "Request result correction"),
        (Perm.FIGHTS_APPROVE, "Approve result correction"), (Perm.BETS_VIEW, "See bets"),
        (Perm.BETS_PLACE, "Place bets"), (Perm.BETS_CANCEL, "Cancel / undo a bet"))),
    ("Bola (number game)", (
        (Perm.BOLA_VIEW, "See draws, results, own tickets"), (Perm.BOLA_PLACE, "Place a Bola bet"),
        (Perm.BOLA_CANCEL, "Cancel a Bola ticket"), (Perm.BOLA_MANAGE, "Create / open / lock / void draws, schedules"),
        (Perm.BOLA_OPEN, "Open / close betting on a draw"), (Perm.BOLA_DECLARE, "Enter the two balls"),
        (Perm.BOLA_REDECLARE, "Request correction"), (Perm.BOLA_APPROVE, "Approve correction"),
        (Perm.BOLA_EXPOSURE, "Stakes, exposure, draw totals"),
        (Perm.BOLA_SKIP, "Skip the draw wait (close betting, draw now)"))),
    ("Scatter (slot)", (
        (Perm.SCATTER_VIEW, "Open Scatter, see own rounds"), (Perm.SCATTER_PLAY, "Spin Scatter with own credit"),
        (Perm.SCATTER_REPORT, "Scatter stakes, wins and house result"))),
    ("Money, cuts & reports", (
        (Perm.COMMISSIONS_VIEW, "See commissions"), (Perm.COMMISSIONS_CLAIM, "Claim commissions"),
        (Perm.REFERRALS_MANAGE, "Manage referral codes"), (Perm.CUTS_VIEW, "See Volume % / SOP % cuts"),
        (Perm.CUTS_MANAGE, "Change cuts"), (Perm.CASHFLOW_VIEW, "House cashflow"),
        (Perm.REPORTS_VIEW, "Report archives"), (Perm.HIDDEN_VIEW, "Hidden layer (house revenue, formula audit)"))),
    ("Admin Panel & security", (
        (Perm.PANEL_VIEW, "Open the Admin Panel"), (Perm.PAWNS_MANAGE, "Access manager (admins only)"),
        (Perm.APPROVALS_DECIDE, "Answer requests for rights (admins only)"),
        (Perm.BISHOPS_MANAGE, "Appoint Bishops (King only)"), (Perm.KING_DESK, "King desk: emergency switches (King only)"),
        (Perm.SETTINGS_VIEW, "See variables"), (Perm.SETTINGS_MANAGE, "Change variables"),
        (Perm.ACCOUNTS_VIEW, "Accounts page"), (Perm.ACCOUNTS_EDIT, "Edit account details"),
        (Perm.ACCOUNTS_REMOVE, "Close accounts"), (Perm.IPBANS_MANAGE, "Ban / unban IPs"),
        (Perm.STREAMS_VIEW, "See livestream sources"), (Perm.STREAMS_MANAGE, "Manage the livestream and videos"),
        (Perm.AUDIT_VIEW, "Audit trail"), (Perm.TOKENS_MANAGE, "Manage tokens"),
        (Perm.CANCEL_LOG, "Cancel Ticket / Bola Cancelled pages"), (Perm.CHAT_USE, "Livestream chat"))),
    ("Staff Panel", (
        (Perm.STAFF_VIEW, "Staff Panel"), (Perm.STAFF_TELLER, "Staff \u2192 Teller section"),
        (Perm.STAFF_DECLARATOR, "Staff \u2192 Declarator section"),
        (Perm.SYSTEM_POOL_VIEW, "System pool on the Teller page"))),
    ("Agent Panel", (
        (Perm.AGENT_PANEL_VIEW, "Agent Panel"),
        (Perm.AGENT_ROOM_CUTS, "Set what Sub Agents earn, per room"))),
)

_PERM_ORDER = {p.value: i for i, p in enumerate(Perm)}
PERM_LABELS: dict[str, str] = {perm.value: label for _cat, items in PERM_CATALOG for perm, label in items}


def normalize_overrides(grants, revokes) -> tuple[list[str], list[str]]:
    """Clean what an editor posted: known keys, grants without the non-delegable ones, stable order. Deny beats allow."""
    deny = {p.value for p in _parse_revoke_keys(revokes)}
    allow = {p.value for p in _parse_perm_keys(grants)} - deny
    order = lambda keys: sorted(keys, key=_PERM_ORDER.__getitem__)  # noqa: E731
    return order(allow), order(deny)


# ---- states: per right "allow", "deny" or "rule" ---------------------------------------------------------

def _apply_layer(state: dict, layer: Layer | None) -> None:
    """Lay one level of settings over the state. Inside a level: allow < rule < deny."""
    if layer is None:
        return
    for perm in layer.grants:
        state[perm] = ("allow", None)
    for perm, rule in layer.rules.items():
        state[perm] = ("rule", rule)
    for perm in layer.revokes:
        state[perm] = ("deny", None)


def builtin_permissions_for(role: Role | str, *, gated: bool = True) -> set[Perm]:
    """A role's rights as written in code, before any change from the King or a Bishop."""
    role = Role(role)
    perms = set(ROLE_PERMS.get(role, set()))
    if gated and bola_dev_only() and role != Role.ADMIN:
        perms -= BOLA_PERMS
    return perms


def role_state(role_key: str) -> dict:
    """A role's starting rights (written in code) with the changes made on its page. Absent = denied."""
    base = builtin_player_perms() if role_key == PLAYER_ROLE_KEY else set(ROLE_PERMS.get(Role(role_key), set()))
    state = {perm: ("allow", None) for perm in base}
    if role_key != Role.ADMIN.value:                       # the King's rights can never be edited
        _apply_layer(state, snapshot().roles.get(role_key))
    return state


def type_state(type_id: int, base_role: str) -> dict:
    state = role_state(base_role)
    _apply_layer(state, snapshot().types.get(type_id))
    return state


def _evaluate(state: dict, now: dt.datetime) -> tuple[set, set, dict]:
    """(allowed now, may start but must ask, limit rules in force) from a state map."""
    allowed: set[Perm] = set()
    ask: set[Perm] = set()
    limits: dict = {}
    for perm, (kind, rule) in state.items():
        if kind == "allow":
            allowed.add(perm)
        elif kind == "rule":
            verdict = rules_mod.rule_state(rule, now)
            if verdict == "allow":
                allowed.add(perm)
            elif verdict == "ask":
                ask.add(perm)
            elif verdict == "limit":
                allowed.add(perm)
                limits[perm] = rule
    return allowed, ask, limits


def _apply_ceiling(role_key: str, allowed: set, ask: set, limits: dict, *, gated: bool = True) -> None:
    """What nobody of this role can hold, whatever was ticked: the King's own rights, what the King sealed from the
    Dev tree, and the number game while it is dev-only. "Ask first" never applies to look-only rights."""
    if role_key != Role.ADMIN.value:
        banned = set(sealed_perms()) if role_key in (Role.BISHOP.value, Role.PAWN.value) else set(KING_ONLY)
        if gated and bola_dev_only():
            banned |= BOLA_PERMS
        allowed -= banned
        ask -= banned
        for perm in banned:
            limits.pop(perm, None)
    ask -= VIEW_PERMS


def permissions_for(role: Role | str, *, gated: bool = True) -> set[Perm]:
    """The role's rights right now: its built-in defaults with the changes made on its page (rules read at this
    moment). A Pawn's own switches are added by permissions_of. `gated=False` ignores the Bola dev-only gate (the
    editors use it so they can show what the gate hides)."""
    key = role.value if isinstance(role, Role) else str(role)
    allowed, ask, limits = _evaluate(role_state(key), rules_mod.utcnow())
    _apply_ceiling(key, allowed, ask, limits, gated=gated)
    return allowed


def default_permissions_for_key(kind_key: str, *, gated: bool = True) -> set[Perm]:
    """Role defaults for a role value or ``player`` (Admin changes applied)."""
    return permissions_for(kind_key, gated=gated)


def type_permissions(type_id: int, base_role: str, *, gated: bool = True) -> set[Perm]:
    """What a custom account type grants by default: its base role's defaults, then the type's own changes."""
    allowed, ask, limits = _evaluate(type_state(type_id, base_role), rules_mod.utcnow())
    _apply_ceiling(base_role, allowed, ask, limits, gated=gated)
    return allowed


def player_perms() -> set[Perm]:
    """What a player may do right now (Admin changes applied; the number game stays hidden while it is dev-only)."""
    return permissions_for(PLAYER_ROLE_KEY)


@dataclass
class Rights:
    """Everything an account may do right now, split by how."""
    allowed: set = field(default_factory=set)       # may do it
    ask: set = field(default_factory=set)           # may start it; the King / a Bishop must approve first
    limits: dict = field(default_factory=dict)      # Perm -> limit rule, for allowed rights that are counted
    frozen: bool = False                            # the King froze this kind of account: look-only

    def mode_of(self, perm: Perm) -> str:
        if perm in self.limits:
            return "limit"
        if perm in self.allowed:
            return "allow"
        return "ask" if perm in self.ask else "deny"


def _account_state(user: User, *, with_account: bool) -> dict:
    snap = snapshot()
    state = role_state(user.role.value)
    for key in user.grant_set:                             # a Pawn's switches
        grant = GRANT_BY_KEY.get(key)
        if grant is not None:
            for perm in grant.perms:
                state[perm] = ("allow", None)
    if user.account_profile_id is not None:
        _apply_layer(state, snap.types.get(user.account_profile_id))
    if with_account:
        _apply_layer(state, user_layer(user))
    return state


def user_layer(user: User) -> Layer:
    """The account's own Allow / Deny / rules, cleaned."""
    return parse_layer(getattr(user, "perm_grants", None), getattr(user, "perm_revokes", None),
                       getattr(user, "perm_rules", None))


def rights_of(user: User, *, now: dt.datetime | None = None) -> Rights:
    """The full answer: role defaults -> Pawn switches -> account type -> the account's own settings -> a running
    pass from an approved request -> the King's ceilings (sealed, frozen, paused). The King and the DEV player
    preview are never overridden."""
    if user.preview_player_id is not None:                 # the DEV 'view as a player' stand-in
        return Rights(player_perms())
    if user.role == Role.ADMIN:
        return Rights(set(Perm))
    now = now or rules_mod.utcnow()
    allowed, ask, limits = _evaluate(_account_state(user, with_account=True), now)
    for perm, until in snapshot().passes.get(user.id, {}).items():   # approved "for a while" passes
        if until > now:
            allowed.add(perm)
            ask.discard(perm)
            limits.pop(perm, None)
    key = user.role.value
    _apply_ceiling(key, allowed, ask, limits)
    frozen = (key == Role.PAWN.value and bool(flag("freeze_pawns"))) or (key == Role.BISHOP.value and bool(flag("freeze_bishops")))
    if frozen:
        allowed = {perm for perm in allowed if perm in VIEW_PERMS}
        ask, limits = set(), {}
    if flag("approvals_paused"):
        ask = set()
    if not frozen:
        allowed |= set(user.__dict__.get("_elevated", ()))     # a single-use pass spent on THIS request
    return Rights(allowed, ask, limits, frozen)


def base_permissions_of(user: User) -> set[Perm]:
    """What the account gets from its role, a Pawn's switches and its account type -- before its own settings."""
    if user.preview_player_id is not None:
        return player_perms()
    if user.role == Role.ADMIN:
        return set(Perm)
    allowed, ask, limits = _evaluate(_account_state(user, with_account=False), rules_mod.utcnow())
    _apply_ceiling(user.role.value, allowed, ask, limits)
    return allowed


def permissions_of(user: User) -> set[Perm]:
    """Everything this account may do right now. "Ask first" rights are NOT in here (the action must be approved
    before it runs, so a check inside a service fails closed); see rights_of / app.services.access."""
    return rights_of(user).allowed



def _groups_with(user: User, suffix: str) -> set[str]:
    """Admin Panel variable groups this account may view / edit. The King: all. A Bishop: all except the groups the
    King sealed. A Pawn: the groups ticked for it, never a sealed one."""
    if user.role == Role.ADMIN:
        return set(GROUPS)
    if user.role == Role.BISHOP:
        found = set(GROUPS) - sealed_groups()
        if bola_dev_only():
            found.discard(BOLA_GROUP)
        return found
    if user.role == Role.PAWN:
        grants = user.grant_set
        found = {g for g in GROUPS if f"vars:{g}:{suffix}" in grants} - sealed_groups()
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


def sees_house(user: User) -> bool:
    """May this account see the House cut and the Payout pool (what is left after the cut)? The Admin always;
    a Pawn (view-only staff) once the Admin switches on 'totals:view'. Everyone else, players included, sees
    only each side's multiple and the total bets."""
    if user.role == Role.ADMIN:
        return True
    if user.role == Role.BISHOP:
        return Perm.HIDDEN_VIEW in permissions_of(user)        # the King can seal the house money from Bishops
    return user.role == Role.PAWN and "totals:view" in user.grant_set


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
    return user.role in TOP_ROLES or user.role in (Role.PAWN, Role.TELLER)


def can_create_role(parent: User, child_role: Role) -> bool:
    """Who may create which account directly (no referral code).

    King   -> Master, Agent, Sub Agent, Declarator, Teller, Pawn, Bishop (anything under an upline the King picks).
    Bishop -> the same, minus Bishop.
    Pawn   -> Agent, Sub Agent, only when the Admin ticked "Create ... accounts".
    Master -> Agent, Declarator.
    Agent  -> Sub Agent.
    (Everyone below the Master recruits through referral codes in practice.)
    There is only ever ONE King; the King and Bishops create Pawns."""
    if parent.status != Status.ACTIVE:
        return False
    if parent.role == Role.ADMIN:
        return child_role in (Role.MASTER, Role.AGENT, Role.SUB_AGENT, Role.DECLARATOR, Role.TELLER, Role.PAWN, Role.BISHOP)
    if parent.role == Role.BISHOP:          # everything the King does, except appointing another Bishop
        return child_role in (Role.MASTER, Role.AGENT, Role.SUB_AGENT, Role.DECLARATOR, Role.TELLER, Role.PAWN)
    if parent.role == Role.PAWN:
        return (child_role in (Role.AGENT, Role.SUB_AGENT)
                and Perm.AGENTS_CREATE in permissions_of(parent))
    if parent.role == Role.MASTER:
        return child_role in (Role.AGENT, Role.DECLARATOR)
    return child_role in CHILD_ROLES.get(Role(parent.role), ())


def creatable_roles(user: User) -> list[Role]:
    """The user types offered on the Create user form (Pawns are made in the Admin Panel)."""
    return [r for r in (Role.MASTER, Role.AGENT, Role.SUB_AGENT, Role.DECLARATOR, Role.TELLER)
            if can_create_role(user, r)]


def _visible_ids(db: Session, user: User) -> set[int]:
    """Every id the operator tier may see: the King sees all; everyone else sees no King and no Bishop."""
    stmt = select(User.id)
    if user.role != Role.ADMIN:
        stmt = stmt.where(User.role.notin_(ADMIN_TIER))
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
    if target is None or (target.is_hidden and actor.role != Role.ADMIN and target.id != actor.id):
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
    if actor.role in (Role.BISHOP, Role.MASTER, Role.PAWN, Role.TELLER):
        return User.role.notin_(ADMIN_TIER)
    return User.parent_id == actor.id
