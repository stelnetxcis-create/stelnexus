"""Store: the Battle Pass and the one-game Chat Token that unlock livestream chat.

Money moves through the same double-entry ledger as everything else: the buyer's wallet is
debited and house revenue is credited, in one balanced transaction, with an idempotency key so a
double-click or a retried request can never charge twice.

Access rules (see ``access``):
  * the Admin, and a Pawn granted ``chat.use``, may always chat (staff);
  * anyone with a running Battle Pass or Lux VIP Battle Pass may chat on every stream (Meron/Wala games, Esports);
  * anyone who spent a Chat Token on a game may chat on that one game. One purchase of tokens works on
    games from at most ``store.token_events`` (3) different events;
  * nobody can chat once the game's result is declared (the room is locked, history stays readable).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.errors import Conflict, Forbidden, ValidationError
from app.core.money import fmt
from app.core.rbac import Perm, permissions_of
from app.core.settings_registry import SettingsStore
from app.db.models import ChatPass, ChatToken, Fight, FightStatus, Status, User
from app.services import accounts, ledger

PASS_PERKS = (
    "Chat on every livestream for the whole pass: Meron/Wala games and Esports",
    "Previous and forward on the live video: rewind a moment you missed, skip ahead, jump back to live",
    "Gold Battle Pass badge next to your name in chat",
    "No tokens to spend: open any stream and just talk",
)


LUX_EXTRAS = (
    "Everything in the Battle Pass: chat on every stream, previous / forward on the video",
    "Flashy, highlighted chat: your messages glow gold with a Lux VIP crest",
    "Your purchase helps keep the server running and maintained. Thank you for backing us",
)

LUX_THANKS = ("From all of us at StelNexus: we humbly thank you for being a Lux VIP. Your support keeps the "
              "servers running, maintained and improving, and we are truly grateful.")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


@dataclass
class StoreState:
    enabled: bool
    pass_price: int
    pass_days: int
    token_price: int
    token_uses: int
    pass_expires: dt.datetime | None
    pass_days_left: int
    tokens: int
    balance: int
    lux_price: int = 0
    lux_days: int = 30
    lux_expires: dt.datetime | None = None
    lux_days_left: int = 0
    token_events: int = 3

    @property
    def has_pass(self) -> bool:
        return self.pass_expires is not None

    @property
    def has_lux(self) -> bool:
        return self.lux_expires is not None


def _until(db: Session, user: User, tier: str | None) -> dt.datetime | None:
    q = select(func.max(ChatPass.expires_at)).where(ChatPass.user_id == user.id)
    if tier:
        q = q.where(ChatPass.tier == tier)
    latest = db.scalar(q)
    if latest is None:
        return None
    latest = _aware(latest)
    return latest if latest > _now() else None


def active_pass_until(db: Session, user: User) -> dt.datetime | None:
    """A running pass of either tier gives the perks."""
    return _until(db, user, None)


def active_lux_until(db: Session, user: User) -> dt.datetime | None:
    return _until(db, user, "lux")


def _days_left(until: dt.datetime | None) -> int:
    return max(1, -(-int((until - _now()).total_seconds()) // 86400)) if until else 0


def token_count(db: Session, user: User) -> int:
    return int(db.scalar(select(func.count(ChatToken.id)).where(
        ChatToken.user_id == user.id, ChatToken.used_at.is_(None))) or 0)


def state(db: Session, user: User, store: SettingsStore) -> StoreState:
    until = active_pass_until(db, user)
    left = _days_left(until)
    lux = active_lux_until(db, user)
    balance = accounts.wallet_for_user(db, user).balance_cents if user.has_wallet else 0
    return StoreState(
        enabled=bool(store.get("store.enabled")),
        pass_price=int(store.get("store.pass_price_cents")), pass_days=int(store.get("store.pass_days")),
        token_price=int(store.get("store.token_price_cents")), token_uses=int(store.get("store.token_uses")),
        pass_expires=until, pass_days_left=left, tokens=token_count(db, user), balance=balance,
        lux_price=int(store.get("store.lux_price_cents")), lux_days=int(store.get("store.lux_days")),
        lux_expires=lux, lux_days_left=_days_left(lux), token_events=int(store.get("store.token_events")))


def _check_buyer(user: User, store: SettingsStore) -> None:
    if not store.get("store.enabled"):
        raise Forbidden("The store is closed right now.")
    if not user.has_wallet:
        raise Forbidden(f"A {user.role_label} account has no balance, so it cannot buy from the store.")
    if user.status != Status.ACTIVE:
        raise Forbidden("Your account is not active.")


def _charge(db: Session, user: User, *, kind: str, cents: int, memo: str, key: str | None, ref_id: int | None):
    wallet = accounts.wallet_for_user(db, user)
    # wallet is a liability (debit lowers it); house revenue is credit-normal (negative entry raises it)
    return ledger.post(db, kind=kind, entries=[(wallet, +cents), (accounts.house(db), -cents)],
                       actor=user, memo=memo, ref_type="user", ref_id=user.id, idempotency_key=key,
                       meta={"store": kind})


def _buy_pass(db: Session, user: User, store: SettingsStore, *, tier: str,
              idempotency_key: str | None = None) -> ChatPass:
    _check_buyer(user, store)
    lux = tier == "lux"
    price = int(store.get("store.lux_price_cents" if lux else "store.pass_price_cents"))
    days = int(store.get("store.lux_days" if lux else "store.pass_days"))
    name = "Lux VIP Battle Pass" if lux else "Battle Pass"
    key = f"store.{tier}:{user.id}:{idempotency_key}" if idempotency_key else None
    if key:
        again = ledger.find_by_key(db, key)
        if again is not None:
            found = db.scalar(select(ChatPass).where(ChatPass.txn_uuid == again.uuid))
            if found is not None:
                return found
    txn = _charge(db, user, kind="store.lux" if lux else "store.pass", cents=price,
                  memo=f"{name} ({days} days)", key=key, ref_id=None)
    start = _until(db, user, tier) or _now()              # stacking: a renewal starts when the old pass ends
    row = ChatPass(user_id=user.id, starts_at=start, expires_at=start + dt.timedelta(days=days),
                   price_cents=price, txn_uuid=txn.uuid, tier=tier)
    db.add(row)
    audit.record(db, user, "store.lux" if lux else "store.pass", entity_type="user", entity_id=user.id,
                 after={"price_cents": price, "days": days, "txn": txn.uuid, "tier": tier})
    db.flush()
    return row


def buy_pass(db: Session, user: User, store: SettingsStore, *, idempotency_key: str | None = None) -> ChatPass:
    return _buy_pass(db, user, store, tier="battle", idempotency_key=idempotency_key)


def buy_lux(db: Session, user: User, store: SettingsStore, *, idempotency_key: str | None = None) -> ChatPass:
    """Lux VIP Battle Pass: same features as the Battle Pass, flashy chat, and a thank-you."""
    return _buy_pass(db, user, store, tier="lux", idempotency_key=idempotency_key)


def buy_token(db: Session, user: User, store: SettingsStore, *, idempotency_key: str | None = None) -> ChatToken:
    """One purchase = ``store.token_uses`` tokens (3 by default); each token unlocks one game."""
    _check_buyer(user, store)
    price, uses = int(store.get("store.token_price_cents")), int(store.get("store.token_uses"))
    key = f"store.token:{user.id}:{idempotency_key}" if idempotency_key else None
    if key:
        again = ledger.find_by_key(db, key)
        if again is not None:
            found = db.scalar(select(ChatToken).where(ChatToken.txn_uuid == again.uuid).order_by(ChatToken.id).limit(1))
            if found is not None:
                return found
    txn = _charge(db, user, kind="store.token", cents=price, memo=f"Chat tokens x{uses}", key=key, ref_id=None)
    share, extra = divmod(price, uses)
    rows = [ChatToken(user_id=user.id, price_cents=share + (extra if i == 0 else 0), txn_uuid=txn.uuid)
            for i in range(uses)]
    db.add_all(rows)
    audit.record(db, user, "store.token", entity_type="user", entity_id=user.id,
                 after={"price_cents": price, "uses": uses, "txn": txn.uuid})
    db.flush()
    return rows[0]


# ---------------------------------------------------------------------------
# Access to one game's chat
# ---------------------------------------------------------------------------

def is_locked(fight: Fight) -> bool:
    """The room closes the moment the result is declared (settled) or the game is voided."""
    return fight.status in (FightStatus.SETTLED, FightStatus.VOID)


def token_for(db: Session, user: User, fight: Fight) -> ChatToken | None:
    return db.scalar(select(ChatToken).where(ChatToken.user_id == user.id,
                                             ChatToken.used_fight_id == fight.id))


def access(db: Session, user: User, fight: Fight) -> dict:
    """Why this person can or cannot type right now. ``reason`` drives the composer in the fight view."""
    if is_locked(fight):
        return {"can_send": False, "reason": "closed", "via": ""}
    if Perm.CHAT_USE in permissions_of(user):
        return {"can_send": True, "reason": "ok", "via": "staff"}
    if active_pass_until(db, user):
        return {"can_send": True, "reason": "ok", "via": "pass"}
    if token_for(db, user, fight):
        return {"can_send": True, "reason": "ok", "via": "token"}
    return {"can_send": False, "reason": "locked", "via": ""}


def has_stream_perks(db: Session, user: User) -> bool:
    """Battle Pass extras on the video player (previous / forward). Staff always have them."""
    return Perm.CHAT_USE in permissions_of(user) or active_pass_until(db, user) is not None


def stream_access(db: Session, user: User, scope: str = "esports") -> dict:
    """Chat access for a stream that is not a Meron/Wala game (Esports and future channels).

    A Battle Pass (or staff access) chats on every stream. Chat Tokens are tied to one Meron/Wala
    game, so they do not apply here.
    """
    if Perm.CHAT_USE in permissions_of(user):
        return {"can_send": True, "reason": "ok", "via": "staff", "scope": scope}
    if active_pass_until(db, user):
        return {"can_send": True, "reason": "ok", "via": "pass", "scope": scope}
    return {"can_send": False, "reason": "locked", "via": "", "scope": scope}


def _event_limit(db: Session) -> int:
    return int(SettingsStore(db).get("store.token_events"))


def _batch_events(db: Session, user: User, txn_uuid: str | None) -> set[int]:
    """The events one purchase of tokens has already been used in."""
    q = (select(Fight.event_id).join(ChatToken, ChatToken.used_fight_id == Fight.id)
         .where(ChatToken.user_id == user.id))
    q = q.where(ChatToken.txn_uuid == txn_uuid) if txn_uuid else q.where(ChatToken.txn_uuid.is_(None))
    return set(db.scalars(q).all())


def spendable_tokens(db: Session, user: User, fight: Fight) -> list[ChatToken]:
    """Unused tokens that may be spent on this game: their purchase is still under the event limit,
    or has already been used in this game's event."""
    limit = _event_limit(db)
    rows = db.scalars(select(ChatToken).where(ChatToken.user_id == user.id, ChatToken.used_at.is_(None))
                      .order_by(ChatToken.id)).all()
    cache: dict[str | None, set[int]] = {}
    out = []
    for r in rows:
        if r.txn_uuid not in cache:
            cache[r.txn_uuid] = _batch_events(db, user, r.txn_uuid)
        used = cache[r.txn_uuid]
        if len(used) < limit or fight.event_id in used:
            out.append(r)
    return out


def tokens_for_fight(db: Session, user: User, fight: Fight) -> int:
    return len(spendable_tokens(db, user, fight))


def use_token(db: Session, user: User, fight: Fight) -> ChatToken:
    """Spend one token to talk on this game. Idempotent: if one is already spent here, it is reused."""
    if is_locked(fight):
        raise Conflict("This game's result is already in, so its chat is closed.")
    already = token_for(db, user, fight)
    if already is not None:
        return already
    options = spendable_tokens(db, user, fight)
    if not options:
        if token_count(db, user):
            raise ValidationError(f"Your chat tokens already covered {_event_limit(db)} events, so they "
                                  "cannot be used on this one. Get a new pack in the Store.")
        raise ValidationError("You have no chat tokens. Buy some in the Store.")
    row = db.scalar(select(ChatToken).where(ChatToken.id == options[0].id).with_for_update())
    row.used_at, row.used_fight_id = _now(), fight.id
    audit.record(db, user, "store.token_used", entity_type="fight", entity_id=fight.id,
                 after={"token_id": row.id})
    db.flush()
    return row


def price_text(cents: int) -> str:
    return fmt(cents)
