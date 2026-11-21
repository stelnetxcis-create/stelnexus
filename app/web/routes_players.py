from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request

from app.core.errors import ValidationError
from app.core.money import to_cents
from app.core.rbac import Perm
from app.db.models import PlayerKind, Status
from app.services import accounts, betting, players, presence
from app.web.paging import paginate
from app.web.deps import Ctx, form_data, flash_redirect, render, require_ctx

router = APIRouter()


def _date(raw: str | None) -> dt.date | None:
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ValidationError("Use a valid date (YYYY-MM-DD).") from exc


@router.get("/clp")
async def clp(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PLAYERS_VIEW)
    search = request.query_params.get("q", "")[:60]
    kind = request.query_params.get("kind") or "online"
    pager, rows = paginate(request, 200, lambda limit, offset: players.list_players(
        ctx.db, ctx.user, kind=PlayerKind(kind) if kind in ("card", "online") else None,
        search=search or None, limit=limit, offset=offset))
    found = accounts.player_wallets(ctx.db, [r.id for r in rows])           # one query for all rows
    balances = {row.id: (found[row.id] if row.id in found else players.wallet(ctx.db, row).balance_cents)
                for row in rows}
    return render(ctx, "clp.html", active="clp", rows=rows, balances=balances, search=search, pager=pager,
                  kind=kind,
                  min_age=int(ctx.store.get("player.min_age")))


@router.post("/clp/register")
async def clp_register(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PLAYERS_CREATE)
    data = await form_data(request)
    limit = to_cents(data["daily_limit"]) if data.get("daily_limit") else None
    if data.get("kind") == "online":
        player = players.register_online_player(
            ctx.db, ctx.user, username=data.get("username", ""), password=data.get("password", ""),
            full_name=data.get("full_name", ""), birth_date=_date(data.get("birth_date")),
            gcash_number=data.get("gcash", ""), address=data.get("address"), settings=ctx.store,
        )
    else:
        player = players.register_card_player(
            ctx.db, ctx.user, card_number=data.get("card_number", ""),
            full_name=data.get("full_name", ""), birth_date=_date(data.get("birth_date")),
            address=data.get("address"), gender=data.get("gender"), settings=ctx.store,
            daily_limit_cents=limit,
        )
    return flash_redirect("/clp", f"Player '{player.full_name}' registered.")


@router.get("/clp/{player_id}/history")
async def clp_history(player_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PLAYERS_VIEW)
    player = players.get_player(ctx.db, ctx.user, player_id)
    from app.services import accounts, ledger
    account = accounts.wallet_for_player(ctx.db, player)
    # Bets and the credit ledger each page on their own parameter, so older bets do not reset the ledger.
    tpager, tickets = paginate(request, 50, lambda limit, offset: betting.list_tickets(
        ctx.db, ctx.user, player_id=player_id, limit=limit, offset=offset), param="tpage")
    lpager, entries = paginate(request, 50, lambda limit, offset: ledger.history(
        ctx.db, account.id, limit=limit, offset=offset), param="lpage")
    return render(ctx, "player_history.html", active="clp", player=player, tickets=tickets,
                  tpager=tpager, lpager=lpager,
                  balance=account.balance_cents, entries=entries,
                  gcash=players.decrypt_field(player.gcash_enc),
                  address=players.decrypt_field(player.address_enc))


@router.post("/players/{player_id}/update")
async def player_update(player_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.PLAYERS_EDIT)
    data = await form_data(request)
    if data.get("self_exclude_days"):
        players.set_self_exclusion(ctx.db, ctx.user, player_id, int(data["self_exclude_days"]), ctx.store)
        return flash_redirect(f"/clp/{player_id}/history", "Self-exclusion applied.")
    players.update_player(
        ctx.db, ctx.user, player_id,
        status=Status(data["status"]) if data.get("status") else None,
        kyc_verified=(data["kyc"] == "1") if data.get("kyc") else None,
        daily_limit_cents=to_cents(data["daily_limit"]) if data.get("daily_limit") else None,
        reason=data.get("reason"),
    )
    return flash_redirect(f"/clp/{player_id}/history", "Player updated.")


# --- who is online (Admin, or a Pawn with the grant) ------------------------

@router.get("/clp/online")
async def clp_online(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.ONLINE_VIEW)
    rows = presence.online_accounts(ctx.db, ctx.user, ctx.store)
    return render(ctx, "online.html", active="clp", rows=rows,
                  window=int(ctx.store.get("presence.window_minutes")))
