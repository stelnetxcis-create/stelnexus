"""Agent Panel (sidebar, below the Staff Panel): the Agent's own desk.

An Agent is a higher tier of the Users branch (player -> sub agent -> AGENT -> master). The panel shows the Agent's
profile and its branch: its Sub Agents and players. Click a Sub Agent and its panel opens, with the games being
played right now and the commission that Sub Agent earns in each room, which the Agent can change. Below that the
Agent can set what ALL its Sub Agents earn per room. A Sub Agent has no one below it, so it has no panel of its own to run. Master opens it,
sets the cuts of the Agents it invited (and of their Sub Agents) and answers the Sub Agent panel requests there; the panel never shows a Master tier: an Agent's ladder is Player, Sub Agent, Agent.
What a Sub Agent keeps to itself (its players and earnings) opens to the Agent only after the Sub Agent allows it and
the Master approves (app.services.panel_access). The dev team (Admin, Bishop) opens the same design rooted at the whole network and can open any Agent from the tree;
there the Admin side may also set that Agent's Volume % per room. There is no separate picker page any more.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from app.core.branches import describe
from app.core.errors import NotFound, ValidationError
from app.core.rbac import Perm, assert_user_in_scope, scope_ids
from app.db.models import Player, Role, User
from app.services import commissions, panel_access
from app.web.deps import Ctx, flash_redirect, form_data, render, require_ctx

router = APIRouter()
PANEL_URL = "/agent-panel"

TIERS = ["Player", "Sub Agent", "Agent"]          # the Agent's ladder: no Master tier is ever shown here
MASTER_TIERS = TIERS + ["Master"]                 # a Master's own panel: the same ladder, one step higher
DEV_TIERS = MASTER_TIERS + ["Admin"]              # the dev team's: the whole network (a Bishop reads "Admin" too)
PLAYER_LEAF_CAP = 60                              # leaves drawn per branch before "and N more"


def _tier(role) -> dict:
    """describe() for the panel: the same facts, but the ladder ends at Agent."""
    d = dict(describe(role))
    d["of"] = len(TIERS)
    d["tier"] = min(d["tier"], len(TIERS))
    return d


def _players_by_owner(db, owner_ids: list[int]) -> dict[int, list[Player]]:
    if not owner_ids:
        return {}
    out: dict[int, list[Player]] = {i: [] for i in owner_ids}
    rows = db.scalars(select(Player).where(Player.agent_id.in_(owner_ids)).order_by(Player.full_name)).all()
    for p in rows:
        out[p.agent_id].append(p)
    return out



def _target(ctx: Ctx, agent_id: str | int | None) -> User | None:
    """The Agent whose panel is shown: the signed-in Agent, or (dev team) the one picked with ?agent=."""
    if ctx.user.role == Role.AGENT:
        return ctx.user
    try:
        wanted = int(agent_id) if agent_id not in (None, "") else None
    except (TypeError, ValueError):
        wanted = None
    if wanted is None:
        return None
    target = ctx.db.get(User, wanted)
    if target is None or target.role != Role.AGENT:
        raise NotFound("Agent not found.")
    return assert_user_in_scope(ctx.db, ctx.user, target.id)


def _url(ctx: Ctx, target: User, sub_id: int | None = None) -> str:
    parts = []
    if ctx.user.role != Role.AGENT:
        parts.append(f"agent={target.id}")
    if sub_id:
        parts.append(f"sub={sub_id}")
    return PANEL_URL + ("?" + "&".join(parts) if parts else "")


def _player_counts(db, agent_ids: list[int]) -> dict[int, int]:
    if not agent_ids:
        return {}
    return {aid: int(n) for aid, n in db.execute(
        select(Player.agent_id, func.count(Player.id)).where(Player.agent_id.in_(agent_ids)).group_by(Player.agent_id)).all()}


def _network_view(ctx: Ctx, root: User, viewer: str, approvals: list):
    """The panel's home for anyone who is not an Agent: the Agent's panel one level (or more) up, with the same hero
    and golden tree. A Master is the root of its own Agents; the dev team is the root of the whole visible network
    (Masters -> Agents -> Sub Agents -> players). Players under a Sub Agent stay hidden until approved, for everyone."""
    db, store = ctx.db, ctx.store
    dev = root.role != Role.MASTER
    if dev:
        users = list(db.scalars(select(User).where(
            User.id.in_(scope_ids(db, root)), User.role.in_([Role.MASTER, Role.AGENT, Role.SUB_AGENT]))
            .order_by(User.full_name, User.username)).all())
        masters = [u for u in users if u.role == Role.MASTER]
        agent_users = [u for u in users if u.role == Role.AGENT]
        sub_users = [u for u in users if u.role == Role.SUB_AGENT]
    else:
        masters = []
        agent_users = list(db.scalars(select(User).where(User.role == Role.AGENT, User.parent_id == root.id)
                                      .order_by(User.full_name, User.username)).all())
        sub_users = list(db.scalars(select(User).where(User.role == Role.SUB_AGENT,
                                                       User.parent_id.in_([a.id for a in agent_users] or [0]))
                                    .order_by(User.full_name, User.username)).all())
    everyone = [root.id] + [m.id for m in masters] + [a.id for a in agent_users] + [k.id for k in sub_users]
    counts = _player_counts(db, everyone)
    access = panel_access.status_by_sub(db, [k.id for k in sub_users])
    owned = _players_by_owner(db, [i for i in everyone if i not in {k.id for k in sub_users}]
                              + [k.id for k in sub_users if access.get(k.id) == "approved"])
    agent_nodes = {}
    for a in agent_users:
        subs = [{"user": k, "players": counts.get(k.id, 0), "d": _tier(k.role), "access": access.get(k.id, ""),
                 "leaves": (owned.get(k.id, []) if access.get(k.id) == "approved" else None),
                 "sop": {g: commissions.effective_sub_bps(k, a, store, g) for g in commissions.GAMES}}
                for k in sub_users if k.parent_id == a.id]
        agent_nodes[a.id] = {"user": a, "d": _tier(a.role), "players": counts.get(a.id, 0), "subs": subs,
                             "leaves": owned.get(a.id, []),
                             "branch_players": counts.get(a.id, 0) + sum(x["players"] for x in subs)}
    master_ids = {m.id for m in masters}
    groups = []
    for m in masters:
        under = [n for n in agent_nodes.values() if n["user"].parent_id == m.id]
        groups.append({"user": m, "nodes": under, "leaves": owned.get(m.id, []), "players": counts.get(m.id, 0),
                       "branch_players": counts.get(m.id, 0) + sum(n["branch_players"] for n in under)})
    nodes = [n for n in agent_nodes.values() if dev is False or n["user"].parent_id not in master_ids]
    if dev:
        totals, profile, ladder = None, {"tier": 5}, DEV_TIERS
    else:
        totals, profile, ladder = commissions.totals_for(db, root), dict(describe(root.role)), MASTER_TIERS
    return render(
        ctx, "agent_panel.html", active="agentpanel", target=None, master_view=True, master=root, root_dev=dev,
        root_label=("Admin" if dev else "Master"), profile=profile, tiers=ladder, ladder_on=len(ladder), master_groups=groups,
        nodes=nodes, direct_players=owned.get(root.id, []), leaf_cap=PLAYER_LEAF_CAP, players_total=sum(counts.values()),
        sub_total=len(sub_users), agent_total=len(agent_users), master_total=len(masters), totals=totals,
        viewer=viewer, approvals=approvals, can_ask=False, back=PANEL_URL)


@router.get(PANEL_URL)
async def agent_panel(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.AGENT_PANEL_VIEW)
    db, store = ctx.db, ctx.store
    target = _target(ctx, request.query_params.get("agent"))

    viewer = {Role.MASTER: "Master", Role.AGENT: "Agent"}.get(ctx.user.role, "dev team")
    approvals = ([m for m in panel_access.inbox_for(db, ctx.user)["approve"]]
                 if ctx.user.role in (Role.MASTER, Role.ADMIN) else [])
    if target is None:                                     # Master and dev team: the whole branch / network as a tree
        return _network_view(ctx, ctx.user, viewer, approvals)

    subs = list(db.scalars(select(User).where(User.role == Role.SUB_AGENT, User.parent_id == target.id)
                           .order_by(User.full_name, User.username)).all())
    counts = _player_counts(db, [target.id] + [k.id for k in subs])
    access = panel_access.status_for(db, target.id, [k.id for k in subs])
    owned = _players_by_owner(db, [target.id] + [k.id for k in subs if access.get(k.id) == "approved"])
    cards = [{"user": k, "players": counts.get(k.id, 0), "d": _tier(k.role), "access": access.get(k.id, ""),
              "leaves": (owned.get(k.id, []) if access.get(k.id) == "approved" else None),
              "sop": {g: commissions.effective_sub_bps(k, target, store, g) for g in commissions.GAMES}} for k in subs]

    sub = None
    sub_rows: list[dict] = []
    sub_totals = None
    raw_sub = request.query_params.get("sub")
    if raw_sub:
        sub = next((k for k in subs if str(k.id) == raw_sub), None)
        if sub is None:
            raise NotFound("That Sub Agent is not in this branch.")
        sub_rows = commissions.sub_ongoing_rows(db, store, target, sub)
        if access.get(sub.id) == "approved":
            sub_totals = commissions.totals_for(db, sub)

    own = ctx.user.id == target.id and ctx.can(Perm.AGENT_ROOM_CUTS)
    mine = ctx.user.role == Role.MASTER and target.parent_id == ctx.user.id and ctx.can(Perm.AGENT_ROOM_CUTS)
    can_sop = own or mine or ctx.can(Perm.CUTS_MANAGE)
    return render(
        ctx, "agent_panel.html", active="agentpanel", target=target, cards=cards, sub=sub, sub_rows=sub_rows,
        profile=_tier(target.role), tiers=TIERS, direct_players=owned.get(target.id, []),
        leaf_cap=PLAYER_LEAF_CAP, sub_totals=sub_totals,
        can_ask=(ctx.user.role == Role.AGENT), viewer=viewer, approvals=approvals, players_total=sum(counts.values()), totals=commissions.totals_for(db, target),
        rooms=commissions.room_rows(db, store, agent=target), games=commissions.GAMES, game_labels=commissions.GAME_LABELS,
        can_sop=can_sop, can_volume=mine or ctx.can(Perm.CUTS_MANAGE),
        max_sop=int(store.get("commission.max_kubrador_cut_bps")), unlimited=ctx.user.is_admin_tier,
        is_dev=(ctx.user.role != Role.AGENT), back=_url(ctx, target), sub_back=_url(ctx, target, sub.id if sub else None))


@router.post(PANEL_URL + "/rooms")
async def agent_panel_save(request: Request, ctx: Ctx = Depends(require_ctx)):
    """Save (or reset, when blank) one room's SOP % for ALL the Agent's Sub Agents. The Admin side may add Volume %."""
    ctx.need(Perm.AGENT_PANEL_VIEW)
    data = await form_data(request)
    target = _target(ctx, data.get("agent_id") or None)
    if target is None:
        raise ValidationError("Pick an Agent first.")
    try:
        location_id = int(data.get("location_id", ""))
    except ValueError:
        raise ValidationError("Pick a room.")
    commissions.set_room_cut(
        ctx.db, ctx.user, game=data.get("game", ""), location_id=location_id, agent_id=target.id,
        volume=data.get("volume") if "volume" in data else None,
        sop=data.get("sop") if "sop" in data else None,
        settings=ctx.store, reason=(data.get("reason") or None))
    return flash_redirect(_url(ctx, target) + "#defaults", "Room cut saved.")


@router.post(PANEL_URL + "/cut")
async def agent_panel_cut(request: Request, ctx: Ctx = Depends(require_ctx)):
    """The OVERALL cut of one account (all rooms): an Agent's Volume % / SOP %, or a Sub Agent's personal SOP %. A room's
    own cut, set below, still beats it. Blank resets to the game's default."""
    ctx.need(Perm.AGENT_PANEL_VIEW)
    data = await form_data(request)
    target = _target(ctx, data.get("agent_id") or None)
    if target is None:
        raise ValidationError("Pick an Agent first.")
    try:
        who = ctx.db.get(User, int(data.get("user_id", "")))
    except ValueError:
        who = None
    if who is None:
        raise NotFound("Account not found.")
    commissions.set_cut(ctx.db, ctx.user, who, volume=data.get("volume") if "volume" in data else None,
                        sop=data.get("sop") if "sop" in data else None, settings=ctx.store,
                        reason=(data.get("reason") or None))
    if who.role == Role.SUB_AGENT:
        return flash_redirect(_url(ctx, target, who.id) + "#sub", f"{who.full_name}'s cut saved.")
    return flash_redirect(_url(ctx, target) + "#cut", f"{who.full_name}'s cut saved.")


@router.post(PANEL_URL + "/subs")
async def agent_panel_sub_save(request: Request, ctx: Ctx = Depends(require_ctx)):
    """Save (or reset, when blank) what ONE Sub Agent earns (SOP %) in one room of one game."""
    ctx.need(Perm.AGENT_PANEL_VIEW)
    data = await form_data(request)
    target = _target(ctx, data.get("agent_id") or None)
    if target is None:
        raise ValidationError("Pick an Agent first.")
    try:
        location_id, sub_id = int(data.get("location_id", "")), int(data.get("sub_id", ""))
    except ValueError:
        raise ValidationError("Pick a Sub Agent and a room.")
    commissions.set_sub_room_cut(ctx.db, ctx.user, game=data.get("game", ""), location_id=location_id, sub_id=sub_id,
                                 sop=data.get("sop", ""), settings=ctx.store, reason=(data.get("reason") or None))
    return flash_redirect(_url(ctx, target, sub_id) + "#sub", "Commission saved for this Sub Agent.")


# --------------------------------------------------------------------------- a Sub Agent's own panel: ask, allow, approve
@router.post(PANEL_URL + "/access/ask")
async def agent_panel_ask(request: Request, ctx: Ctx = Depends(require_ctx)):
    """The Agent asks to open one Sub Agent's panel. Nothing shows until the Sub Agent allows and the Master approves."""
    ctx.need(Perm.AGENT_PANEL_VIEW)
    data = await form_data(request)
    if ctx.user.role != Role.AGENT:
        raise ValidationError("Only the Agent asks for this.")
    try:
        sub = ctx.db.get(User, int(data.get("sub_id", "")))
    except ValueError:
        sub = None
    if sub is None:
        raise NotFound("Sub Agent not found.")
    panel_access.ask(ctx.db, ctx.user, sub)
    return flash_redirect(_url(ctx, ctx.user, sub.id) + "#sub", "Asked. The Sub Agent answers first, then the Master approves.")


def _inbox_user(ctx: Ctx) -> User:
    if ctx.user.role not in (Role.SUB_AGENT, Role.MASTER, Role.ADMIN):
        raise NotFound("Nothing here.")
    return ctx.user


@router.get(PANEL_URL + "/consents")
async def agent_panel_consents(request: Request, ctx: Ctx = Depends(require_ctx)):
    """Where a Sub Agent answers an Agent's ask, and where a Master (or the Admin) approves what a Sub Agent allowed."""
    user = _inbox_user(ctx)
    return render(ctx, "agent_consents.html", active="agentpanel", inbox=panel_access.inbox_for(ctx.db, user),
                  is_sub=user.role == Role.SUB_AGENT)


async def _wants_panel(request: Request) -> bool:
    """The Master answers from inside the Agent Panel; a Sub Agent answers on the Panel Requests page."""
    return (await form_data(request)).get("back") == "panel"


@router.post(PANEL_URL + "/consents/{row_id}/{action}")
async def agent_panel_consent_answer(row_id: int, action: str, request: Request, ctx: Ctx = Depends(require_ctx)):
    user = _inbox_user(ctx)
    if action in ("allow", "decline") and user.role == Role.SUB_AGENT:
        panel_access.sub_answer(ctx.db, user, row_id, allow=(action == "allow"))
        message = "Allowed. The Master has been asked to approve." if action == "allow" else "Declined."
    elif action in ("approve", "reject") and user.role in (Role.MASTER, Role.ADMIN):
        panel_access.approver_answer(ctx.db, user, row_id, approve=(action == "approve"))
        message = "Approved. The Agent can open the panel now." if action == "approve" else "Not approved."
    elif action == "revoke":
        panel_access.revoke(ctx.db, user, row_id)
        message = "Access withdrawn."
    else:
        raise NotFound("Unknown action.")
    back = PANEL_URL if (await _wants_panel(request)) else PANEL_URL + "/consents"
    return flash_redirect(back, message)
