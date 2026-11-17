"""Store (Battle Pass + Chat Token) and the livestream chat inside the fight view.

The chat is read-only for everybody who can open the fight view. Typing needs one of: staff
access, a running Battle Pass, or a Chat Token spent on that game. When the declarator announces
the result the room locks for good; the history stays readable.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.core import ratelimit
from app.core.errors import ValidationError
from app.core.rbac import Perm
from app.db.models import ChatMessage, ChatPass, Fight
from app.services import events, store as store_svc
from app.web.deps import Ctx, flash_redirect, form_data, render, require_ctx

router = APIRouter()

CHAT_LIMIT = 100          # messages returned per poll
HISTORY = 80              # messages shown when the room is first opened
SLOW_MODE_SECONDS = 2
ROLE_TONE = {"admin": "gold", "master": "purple", "pawn": "blue", "declarator": "green",
             "cavo": "amber", "kubrador": "amber"}


def _wants_json(request: Request) -> bool:
    return request.headers.get("accept", "").startswith("application/json") or \
        "json" in request.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

@router.get("/store")
async def store_page(ctx: Ctx = Depends(require_ctx)):
    st = store_svc.state(ctx.db, ctx.user, ctx.store)
    return render(ctx, "store.html", active="store", st=st, perks=store_svc.PASS_PERKS, lux_perks=store_svc.LUX_EXTRAS, lux_thanks=store_svc.LUX_THANKS,
                  can_buy=ctx.user.has_wallet and st.enabled)


@router.post("/store/pass")
async def buy_pass(request: Request, ctx: Ctx = Depends(require_ctx)):
    data = await form_data(request)
    row = store_svc.buy_pass(ctx.db, ctx.user, ctx.store, idempotency_key=data.get("idempotency_key") or None)
    return flash_redirect(_back(data), f"Battle Pass active until {row.expires_at:%b %d, %Y}. Chat is unlocked on every stream.")


@router.post("/store/lux")
async def buy_lux(request: Request, ctx: Ctx = Depends(require_ctx)):
    data = await form_data(request)
    row = store_svc.buy_lux(ctx.db, ctx.user, ctx.store, idempotency_key=data.get("idempotency_key") or None)
    return flash_redirect(_back(data), f"Lux VIP active until {row.expires_at:%b %d, %Y}. {store_svc.LUX_THANKS}")


@router.post("/store/token")
async def buy_token(request: Request, ctx: Ctx = Depends(require_ctx)):
    data = await form_data(request)
    store_svc.buy_token(ctx.db, ctx.user, ctx.store, idempotency_key=data.get("idempotency_key") or None)
    n = int(ctx.store.get("store.token_uses"))
    return flash_redirect(_back(data), f"{n} chats added. Spend one on a game to talk on it.")


def _back(data: dict) -> str:
    target = data.get("next") or "/store"
    return target if target.startswith("/") and not target.startswith("//") else "/store"


# ---------------------------------------------------------------------------
# Livestream chat in the fight view
# ---------------------------------------------------------------------------

def _fight(ctx: Ctx, fight_id: int) -> Fight:
    ctx.need(Perm.FIGHTS_VIEW)
    return events.get_fight(ctx.db, fight_id)


def _pass_holders(ctx: Ctx, user_ids: set[int]) -> dict[int, str]:
    """user id -> "lux" or "battle" for everyone in the list with a running pass."""
    if not user_ids:
        return {}
    now = dt.datetime.now(dt.timezone.utc)
    rows = ctx.db.execute(select(ChatPass.user_id, ChatPass.tier).where(
        ChatPass.user_id.in_(user_ids), ChatPass.expires_at > now)).all()
    out: dict[int, str] = {}
    for uid, tier in rows:
        if out.get(uid) != "lux":
            out[uid] = tier or "battle"
    return out


def _messages_json(ctx: Ctx, rows: list[ChatMessage]) -> list[dict]:
    holders = _pass_holders(ctx, {m.user_id for m in rows})
    out = []
    for m in rows:
        name = ctx.who(m.user)
        system = name == "System"
        out.append({
            "id": m.id, "user": name, "initial": (name[:1] or "?").upper(),
            "role": "Management" if system else m.user.role_label,
            "tone": "gold" if system else ROLE_TONE.get(str(m.user.role), "slate"),
            "pass": m.user_id in holders, "lux": holders.get(m.user_id) == "lux", "mine": m.user_id == ctx.user.id,
            "body": m.body, "at": m.created_at.strftime("%H:%M"),
        })
    return out


def _result_json(fight: Fight) -> dict | None:
    """What the declarator announced. Only present once the room is locked."""
    if not store_svc.is_locked(fight):
        return None
    res = fight.result.value if fight.result else "canceled"
    if res == "meron":
        label = fight.entry_name_meron
    elif res == "wala":
        label = fight.entry_name_wala
    elif res == "draw":
        label = "Draw: stakes refunded"
    else:
        label = "Game cancelled: stakes refunded"
    return {"side": res, "label": label}


def _state(ctx: Ctx, fight: Fight) -> dict:
    acc = store_svc.access(ctx.db, ctx.user, fight)
    st = store_svc.state(ctx.db, ctx.user, ctx.store)
    return {
        "fight": {"id": fight.id, "n": fight.number, "st": fight.status.value},
        "locked": store_svc.is_locked(fight), "result": _result_json(fight),
        "can_send": acc["can_send"], "reason": acc["reason"], "via": acc["via"],
        "tokens": store_svc.tokens_for_fight(ctx.db, ctx.user, fight), "has_pass": st.has_pass, "has_lux": st.has_lux, "pass_days_left": st.pass_days_left,
        "store_open": st.enabled and ctx.user.has_wallet,
        "chatters": int(ctx.db.scalar(select(func.count(func.distinct(ChatMessage.user_id)))
                                     .where(ChatMessage.fight_id == fight.id)) or 0),
    }


@router.get("/fights/{fight_id}/chat")
async def chat_poll(fight_id: int, after: int = 0, ctx: Ctx = Depends(require_ctx)):
    fight = _fight(ctx, fight_id)
    q = select(ChatMessage).where(ChatMessage.fight_id == fight.id)
    if after > 0:
        rows = list(ctx.db.scalars(q.where(ChatMessage.id > after).order_by(ChatMessage.id).limit(CHAT_LIMIT)).all())
    else:
        rows = list(reversed(list(ctx.db.scalars(q.order_by(ChatMessage.id.desc()).limit(HISTORY)).all())))
    return JSONResponse({"messages": _messages_json(ctx, rows), **_state(ctx, fight)},
                        headers={"Cache-Control": "no-store"})


@router.post("/fights/{fight_id}/chat/send")
async def chat_send(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    fight = _fight(ctx, fight_id)
    acc = store_svc.access(ctx.db, ctx.user, fight)
    if not acc["can_send"]:
        raise ValidationError("This game's chat is closed: the result has been announced."
                              if acc["reason"] == "closed"
                              else "Chat is locked. Get a Battle Pass or Chat Tokens in the Store.")
    if "json" in request.headers.get("content-type", ""):
        try:
            data = await request.json()
        except ValueError as exc:
            raise ValidationError("Invalid message.") from exc
        data = data if isinstance(data, dict) else {}
    else:
        data = await form_data(request)
    body = " ".join(str(data.get("body") or "").split())[:300]
    if not body:
        raise ValidationError("Type a message first.")
    ratelimit.check(f"fightchat:{ctx.user.id}", 40, 60)
    last = ctx.db.scalar(select(func.max(ChatMessage.created_at)).where(
        ChatMessage.user_id == ctx.user.id, ChatMessage.fight_id == fight.id))
    if last is not None:
        last = last if last.tzinfo else last.replace(tzinfo=dt.timezone.utc)
        if (dt.datetime.now(dt.timezone.utc) - last).total_seconds() < SLOW_MODE_SECONDS:
            raise ValidationError("Slow down a little.")
    msg = ChatMessage(user_id=ctx.user.id, body=body, fight_id=fight.id)
    ctx.db.add(msg)
    ctx.db.flush()
    return JSONResponse({"ok": True, "id": msg.id})


@router.post("/fights/{fight_id}/chat/unlock")
async def chat_unlock(fight_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    """Spend one Chat Token to talk on this game."""
    fight = _fight(ctx, fight_id)
    store_svc.use_token(ctx.db, ctx.user, fight)
    if _wants_json(request):
        return JSONResponse({"ok": True, **_state(ctx, fight)}, headers={"Cache-Control": "no-store"})
    return flash_redirect(f"/change_name?event_id={fight.event_id}&fight_id={fight.id}",
                          f"Chat unlocked for game #{fight.number}.")
