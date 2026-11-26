"""Staff Panel -> People: ONE directory instead of four pages.

It replaces the sidebar's Users and Players pages, the Online-now list and the Admin Panel's Accounts page:

    /staff/people?tab=team      the selling network (was /agents)
    /staff/people?tab=players   the players (was /clp)
    /staff/people?tab=online    who is signed in right now (was /clp/online)
    account rename / remove     now sit inside each person's row (was /admin/accounts)

The old addresses still work and open the matching tab, so bookmarks, links and the API of the forms keep
working. Each old address keeps the permission it always had.

WHO SEES WHAT (decided here, on the server, never by hiding buttons):

    Admin / Bishop   every tab, every column (credit, commission, ref code, GCash in full, device and IP).
    Pawn             only the tabs its grants open; GCash numbers masked; actions only when granted.
    Master           Team only: its own network, balances included. No players, no online list.
    Teller           Online only, and only the name, type, last activity and GCash number. Never the team,
                     the players, balances, IP address or device.
    Everyone else    nothing (Agents, Sub Agents, Declarators, players).

Money columns also follow ``hides_totals``: an account that may not see totals never gets a balance.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.services import badges as _badges
from app.core.branches import describe
from app.core.errors import Forbidden
from app.core.rbac import Perm, creatable_roles, hides_totals, scope_ids
from app.core.security import mask, pii
from app.db.models import PlayerKind, Role
from app.services import account_admin, accounts, players, presence
from app.services.identity import masked_gmail
from app.web.deps import Ctx, render, require_ctx
from app.web.paging import make_pager, page_number, paginate
from app.web.routes_agents import _uplines

router = APIRouter()

PEOPLE_URL = "/staff/people"
TAB_LABELS = {"team": "Team", "players": "Players", "online": "Online now"}
LITE_ROLES = (Role.TELLER,)           # accounts that only get the short online list (name, type, seen, GCash)


# --------------------------------------------------------------------------- who may open what
def tabs_for(ctx: Ctx) -> list[str]:
    """The tabs this account may open, in display order."""
    if ctx.user is None:
        return []
    out: list[str] = []
    if ctx.user.role not in LITE_ROLES and (ctx.can(Perm.AGENTS_VIEW) or ctx.can(Perm.ACCOUNTS_VIEW)):
        out.append("team")
    if ctx.can(Perm.PLAYERS_VIEW) or ctx.can(Perm.ACCOUNTS_VIEW):
        out.append("players")
    if ctx.can(Perm.ONLINE_VIEW):
        out.append("online")
    return out


def people_visible(ctx: Ctx) -> bool:
    return bool(tabs_for(ctx))


def people_nav(ctx: Ctx) -> bool:
    """Sidebar link: only for accounts that have People but no Staff Panel (a Master, a Pawn). Everyone else
    reaches it through Staff Panel -> People."""
    return people_visible(ctx) and not ctx.can(Perm.STAFF_VIEW)


def _gcash(viewer, enc: str | None) -> str | None:
    """A GCash number as this viewer may read it: in full for the Admin tier and a Teller, masked for others."""
    if not enc:
        return None
    number = pii.decrypt(enc)
    if not number:
        return None
    return number if (viewer.is_admin_tier or viewer.role == Role.TELLER) else mask(number)


# --------------------------------------------------------------------------- tab data
def _team(request: Request, ctx: Ctx, q: str) -> dict:
    db, user = ctx.db, ctx.user
    creatable = creatable_roles(user)
    if ctx.can(Perm.AGENTS_VIEW):
        pager, rows = paginate(request, 200, lambda limit, offset: _list_agents(ctx, q, limit, offset))
        hide = hides_totals(user)                       # a Pawn without 'see stakes, pools and payouts' sees no balances
        bal = accounts.user_balances(db, [] if hide else rows)          # one query, not two per row
        wallets = {r.id: (bal[r.id][0] if not hide and r.id in bal else None) for r in rows}
        commissions = {r.id: (bal[r.id][1] if not hide and r.id in bal else None) for r in rows}
        downline = sorted(scope_ids(db, user) - {user.id})
    else:                                               # accounts-only Pawn: names and status, never balances
        per, page = 100, page_number(request, "upage")
        found = account_admin.list_accounts(db, search=q or None, kind="users", limit=per + 1,
                                            user_offset=page * per)["users"]
        pager = make_pager(request, per, page, min(len(found), per), len(found) > per, "upage")
        rows = [u for u in found[:per] if user.is_admin_tier or u.role != Role.PAWN]
        hide, wallets, commissions, downline = True, {}, {}, []
    online_ids: set[int] = set()
    if ctx.can(Perm.ONLINE_VIEW):
        online_ids = {r["user"].id for r in presence.online_accounts(db, user, ctx.store)}
    return dict(rows=rows, pager=pager, wallets=wallets, commissions=commissions, hide_balances=hide,
                creatable=creatable, uplines=_uplines(ctx, creatable), downline=downline, online_ids=online_ids,
                gcash={r.id: _gcash(user, r.gcash_enc) for r in rows if user.is_admin_tier},
                can_account_edit=ctx.can(Perm.ACCOUNTS_EDIT), can_account_remove=ctx.can(Perm.ACCOUNTS_REMOVE),
                can_profile=ctx.can(Perm.AGENTS_EDIT), me=user.id, gmail_mask=masked_gmail,
                badge_map=_badges.showcase_for(db, [r.id for r in rows], ctx.store))


def _list_agents(ctx: Ctx, q: str, limit: int, offset: int):
    from app.services import agents
    return agents.list_agents(ctx.db, ctx.user, search=q or None, limit=limit, offset=offset)


def _players(request: Request, ctx: Ctx, q: str) -> dict:
    db, user = ctx.db, ctx.user
    kind = request.query_params.get("kind") or "online"
    if ctx.can(Perm.PLAYERS_VIEW):
        pager, rows = paginate(request, 200, lambda limit, offset: players.list_players(
            db, user, kind=PlayerKind(kind) if kind in ("card", "online") else None,
            search=q or None, limit=limit, offset=offset))
        found = accounts.player_wallets(db, [r.id for r in rows])         # one query for all rows
        balances = {r.id: (found[r.id] if r.id in found else players.wallet(db, r).balance_cents) for r in rows}
    else:                                               # accounts-only Pawn: no balances
        per, page = 100, page_number(request, "ppage")
        found = account_admin.list_accounts(db, search=q or None, kind="players", limit=per + 1,
                                            player_offset=page * per)["players"]
        pager = make_pager(request, per, page, min(len(found), per), len(found) > per, "ppage")
        rows, balances = found[:per], {}
    return dict(rows=rows, pager=pager, balances=balances, kind=kind, min_age=int(ctx.store.get("player.min_age")),
                gcash={r.id: _gcash(user, r.gcash_enc) for r in rows},
                can_history=ctx.can(Perm.PLAYERS_VIEW), can_account_edit=ctx.can(Perm.ACCOUNTS_EDIT),
                can_account_remove=ctx.can(Perm.ACCOUNTS_REMOVE), gmail_mask=masked_gmail)


def _online(ctx: Ctx) -> dict:
    user = ctx.user
    rows = presence.online_accounts(ctx.db, user, ctx.store)
    return dict(rows=rows, window=int(ctx.store.get("presence.window_minutes")),
                lite=user.role in LITE_ROLES,                    # a Teller: no device, IP or session count
                gcash={r["user"].id: _gcash(user, r["user"].gcash_enc) for r in rows},
                badge_map=_badges.showcase_for(ctx.db, [r["user"].id for r in rows], ctx.store))


# --------------------------------------------------------------------------- the page
def _staff_tabs(ctx: Ctx) -> list:
    from app.web.routes_staff import staff_tabs
    return staff_tabs(ctx) if ctx.can(Perm.STAFF_VIEW) else []


async def people_page(request: Request, ctx: Ctx, tab: str | None):
    tabs = tabs_for(ctx)
    if not tabs:
        raise Forbidden("Your role cannot open People.")
    tab = tab if tab in tabs else tabs[0]                         # an old address whose tab you may not open lands on yours
    q = request.query_params.get("q", "")[:60]
    data = {"team": lambda: _team(request, ctx, q), "players": lambda: _players(request, ctx, q),
            "online": lambda: _online(ctx)}[tab]()
    staff_tabs = _staff_tabs(ctx)
    return render(ctx, "people.html", active="staff" if staff_tabs else "people",
                  staff_tabs=staff_tabs, staff_active="people", tab=tab, describe=describe,
                  people_tabs=[(k, TAB_LABELS[k]) for k in tabs], search=q, **data)


@router.get(PEOPLE_URL)
async def people(request: Request, ctx: Ctx = Depends(require_ctx)):
    return await people_page(request, ctx, request.query_params.get("tab"))


# The old addresses. Each keeps the right it always needed, then opens the matching tab.
@router.get("/agents")
async def agents_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.AGENTS_VIEW)
    return await people_page(request, ctx, "team")


@router.get("/clp")
async def clp(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PLAYERS_VIEW)
    return await people_page(request, ctx, "players")


@router.get("/clp/online")
async def clp_online(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.ONLINE_VIEW)
    return await people_page(request, ctx, "online")


@router.get("/admin/accounts")
async def accounts_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.ACCOUNTS_VIEW)
    kind = request.query_params.get("kind")
    return await people_page(request, ctx, "players" if kind == "players" else "team")
