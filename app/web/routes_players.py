from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request

from app.core.errors import ValidationError
from app.core.money import to_cents
from app.core.rbac import Perm
from app.db.models import PlayerKind, Status
from app.services import betting, players
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
    return flash_redirect("/staff/people?tab=players", f"Player '{player.full_name}' registered.")


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
