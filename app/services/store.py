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

Slow mode: Battle Pass and Chat Token chatters wait ``store.slow_mode_seconds`` between messages. Lux VIP
and staff are never slowed (see ``slow_mode_seconds``).

Buying rules (the server refuses what the Store greys out, so a stray click can never waste money):
  * Lux VIP running  -> no Battle Pass (Lux already includes it) and no Chat Tokens;
  * Battle Pass running -> no Chat Tokens (the pass already covers every game). Lux is still an upgrade.

Consumables (Chat Tokens, Alias Tokens) are shown in the Store as materials. An Alias Token is spent
once to set the name shown in chat instead of the username.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.errors import Conflict, Forbidden, ValidationError
from app.core.money import fmt
from app.core.rbac import Perm, permissions_of
from app.core.settings_registry import SettingsStore
from app.db.models import (
    AliasToken, ChatAlias, ChatPass, ChatToken, Fight, FightStatus, LedgerTxn, Role, Status, User,
)
from app.services import accounts, ledger
from app.services import events as events_svc

PASS_PERKS = (
    "Chat on every livestream for the whole pass: Meron/Wala games and Esports",
    "Previous and forward on the live video: rewind a moment you missed, skip ahead, jump back to live",
    "Gold Battle Pass badge next to your name in chat",
    "No tokens to spend: open any stream and just talk",
    "Chat has slow mode for Battle Pass holders: one message every few seconds",
)


LUX_EXTRAS = (
    "Everything in the Battle Pass: chat on every stream, previous / forward on the video",
    "No slow mode: send as fast as you can type, nobody throttles a Lux VIP",
    "Flashy, highlighted chat: your messages glow gold with a Lux VIP crest",
    "Your purchase helps keep the server running and maintained. Thank you for backing us",
)

LUX_THANKS = ("From all of us at StelNexus: we humbly thank you for being a Lux VIP. Your support keeps the "
              "servers running, maintained and improving, and we are truly grateful.")


PASS_BLOCKED = "Your Lux VIP already includes everything in the Battle Pass."
TOKEN_BLOCKED_LUX = "Your Lux VIP already lets you chat on every game, so you do not need tokens."
TOKEN_BLOCKED_PASS = "Your Battle Pass already lets you chat on every game, so you do not need tokens."


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
    alias_price: int = 0
    alias_tokens: int = 0
    alias: str | None = None
    slow_seconds: int = 3
    battle_expires: dt.datetime | None = None      # the Battle Pass tier alone (``pass_*`` is "any tier")
    battle_days_left: int = 0

    @property
    def has_battle(self) -> bool:
        return self.battle_expires is not None

    @property
    def has_pass(self) -> bool:
        return self.pass_expires is not None

    @property
    def has_lux(self) -> bool:
        return self.lux_expires is not None

    @property
    def pass_block(self) -> str:
        """Why the Battle Pass cannot be bought right now ('' = it can)."""
        return PASS_BLOCKED if self.has_lux else ""

    @property
    def token_block(self) -> str:
        """Why Chat Tokens cannot be bought right now ('' = they can)."""
        if self.has_lux:
            return TOKEN_BLOCKED_LUX
        return TOKEN_BLOCKED_PASS if self.has_pass else ""


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
    battle = _until(db, user, "battle")
    balance = accounts.wallet_for_user(db, user).balance_cents if user.has_wallet else 0
    return StoreState(
        enabled=bool(store.get("store.enabled")),
        pass_price=int(store.get("store.pass_price_cents")), pass_days=int(store.get("store.pass_days")),
        token_price=int(store.get("store.token_price_cents")), token_uses=int(store.get("store.token_uses")),
        pass_expires=until, pass_days_left=left, tokens=token_count(db, user), balance=balance,
        lux_price=int(store.get("store.lux_price_cents")), lux_days=int(store.get("store.lux_days")),
        lux_expires=lux, lux_days_left=_days_left(lux), token_events=int(store.get("store.token_events")),
        alias_price=int(store.get("store.alias_price_cents")), alias_tokens=alias_token_count(db, user),
        alias=alias_of(db, user.id), slow_seconds=int(store.get("store.slow_mode_seconds")),
        battle_expires=battle, battle_days_left=_days_left(battle))


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
    if not lux and active_lux_until(db, user):
        raise Conflict(PASS_BLOCKED)
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
    if active_lux_until(db, user):
        raise Conflict(TOKEN_BLOCKED_LUX)
    if active_pass_until(db, user):
        raise Conflict(TOKEN_BLOCKED_PASS)
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
    # Same order as betting: chat is only available on the current game while it has not started.
    # "paused" = it is on air; "waiting" = an earlier game is still to be done.
    phase = events_svc.stream_phase_of(db, fight)
    if phase in ("onair", "waiting"):
        return {"can_send": False, "reason": "paused" if phase == "onair" else "waiting", "via": ""}
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
    if events_svc.stream_phase_of(db, fight) in ("onair", "waiting"):
        raise Conflict("Chat is not open for this game right now: it is on air or an earlier game is still to be done.")
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


# ---------------------------------------------------------------------------
# Slow mode
# ---------------------------------------------------------------------------

def slow_mode_seconds(db: Session, user: User, store: SettingsStore | None = None) -> int:
    """Seconds this person must wait between chat messages. Lux VIP and staff are never slowed."""
    if Perm.CHAT_USE in permissions_of(user) or active_lux_until(db, user):
        return 0
    return int((store or SettingsStore(db)).get("store.slow_mode_seconds"))


# ---------------------------------------------------------------------------
# Alias: a one-time consumable that sets the name shown in chat instead of the username
# ---------------------------------------------------------------------------

ALIAS_RULES = (
    "One-time use: the token is consumed the moment you set your alias",
    "Your alias shows in livestream chat instead of your username",
    "Changing it later uses another token. Your account stays on record for staff",
    "3 to 20 letters, numbers, spaces, dots, dashes or underscores. No staff or system look-alikes",
)
_ALIAS_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._\-]{1,18}[A-Za-z0-9]$")
_RESERVED = ("admin", "system", "staff", "management", "moderator", "declarator", "stelnexus", "support",
             "official", "master", "pawn")


# Only these account types can see which account is behind an alias. Everyone else just sees the alias.
ALIAS_REVEAL_ROLES = (Role.ADMIN, Role.PAWN)


def can_see_through_alias(user: User) -> bool:
    return user.role in ALIAS_REVEAL_ROLES


def alias_key(alias: str) -> str:
    return re.sub(r"[\s._\-]", "", alias.lower())


def alias_token_count(db: Session, user: User) -> int:
    return int(db.scalar(select(func.count(AliasToken.id)).where(
        AliasToken.user_id == user.id, AliasToken.used_at.is_(None))) or 0)


def alias_of(db: Session, user_id: int) -> str | None:
    return db.scalar(select(ChatAlias.alias).where(ChatAlias.user_id == user_id))


def aliases_for(db: Session, user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    return {uid: a for uid, a in db.execute(select(ChatAlias.user_id, ChatAlias.alias)
                                            .where(ChatAlias.user_id.in_(user_ids))).all()}


def buy_alias_token(db: Session, user: User, store: SettingsStore, *, idempotency_key: str | None = None) -> AliasToken:
    _check_buyer(user, store)
    price = int(store.get("store.alias_price_cents"))
    key = f"store.alias:{user.id}:{idempotency_key}" if idempotency_key else None
    if key:
        again = ledger.find_by_key(db, key)
        if again is not None:
            found = db.scalar(select(AliasToken).where(AliasToken.txn_uuid == again.uuid))
            if found is not None:
                return found
    txn = _charge(db, user, kind="store.alias", cents=price, memo="Alias token", key=key, ref_id=None)
    row = AliasToken(user_id=user.id, price_cents=price, txn_uuid=txn.uuid)
    db.add(row)
    audit.record(db, user, "store.alias", entity_type="user", entity_id=user.id,
                 after={"price_cents": price, "txn": txn.uuid})
    db.flush()
    return row


def clean_alias(db: Session, user: User, raw: str) -> tuple[str, str]:
    """Validate a wanted alias. Returns (alias, key) or raises ValidationError with a plain reason."""
    alias = " ".join((raw or "").split())
    if not _ALIAS_OK.match(alias):
        raise ValidationError("An alias is 3 to 20 characters: letters, numbers, spaces, dots, dashes or "
                              "underscores, starting and ending with a letter or number.")
    key = alias_key(alias)
    if any(word in key for word in _RESERVED):
        raise ValidationError("That alias looks like a staff or system name. Pick another one.")
    others = db.scalars(select(User.username).where(
        func.lower(User.username).like(key[:1] + "%"), User.id != user.id)).all()
    if any(u.lower() == alias.lower() or alias_key(u) == key for u in others):
        raise ValidationError("That name belongs to another account. Pick another alias.")
    clash = db.scalar(select(ChatAlias).where(ChatAlias.alias_key == key, ChatAlias.user_id != user.id))
    if clash is not None:
        raise ValidationError("Someone already uses that alias. Pick another one.")
    return alias, key


def set_alias(db: Session, user: User, store: SettingsStore, raw: str) -> str:
    """Spend one Alias Token to set the alias. Nothing is spent if the alias is refused."""
    alias, key = clean_alias(db, user, raw)
    token = db.scalar(select(AliasToken).where(AliasToken.user_id == user.id, AliasToken.used_at.is_(None))
                      .order_by(AliasToken.id).limit(1).with_for_update())
    if token is None:
        raise ValidationError("You have no alias tokens. Buy one in the Store.")
    current = db.scalar(select(ChatAlias).where(ChatAlias.user_id == user.id))
    if current is not None and current.alias == alias:
        raise ValidationError("That is already your alias. Nothing was spent.")
    token.used_at = _now()
    before = current.alias if current else None
    if current is None:
        db.add(ChatAlias(user_id=user.id, alias=alias, alias_key=key))
    else:
        current.alias, current.alias_key, current.updated_at = alias, key, _now()
    audit.record(db, user, "store.alias_set", entity_type="user", entity_id=user.id,
                 before={"alias": before}, after={"alias": alias, "token_id": token.id})
    db.flush()
    return alias


# ---------------------------------------------------------------------------
# DEV: revoke the effect of a purchase (Admin only, called from /dev/store)
# ---------------------------------------------------------------------------

def effects_of(db: Session, user: User) -> dict:
    """Everything the Store currently gives this account, for the DEV revoke screen."""
    return {
        "battle_until": _until(db, user, "battle"), "lux_until": active_lux_until(db, user),
        "tokens_unused": token_count(db, user), "alias_tokens": alias_token_count(db, user),
        "alias": alias_of(db, user.id),
    }


def revoke_effect(db: Session, admin: User, user: User, what: str, *, reason: str, refund: bool = False) -> str:
    """Take a purchased effect away. ``what``: battle, lux, tokens, alias_tokens, alias.

    Without ``refund`` no money moves (the effect is simply removed). With it, the matching purchases'
    ledger entries are reversed so the player gets their credit back. Always audited with a reason."""
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Give a reason: it is written to the audit log.")
    now, note = _now(), ""
    refunded: set[str] = set()

    def refund_txn(uuid: str | None) -> None:
        if not refund or not uuid or uuid in refunded:
            return
        txn = db.scalar(select(LedgerTxn).where(LedgerTxn.uuid == uuid))
        if txn is not None and txn.reversed_by_txn_id is None:
            ledger.reverse(db, txn, actor=admin, reason=reason, memo=f"Store revoke: {what}")
            refunded.add(uuid)

    if what in ("battle", "lux"):
        rows = db.scalars(select(ChatPass).where(ChatPass.user_id == user.id, ChatPass.tier == what,
                                                 ChatPass.expires_at > now)).all()
        if not rows:
            raise ValidationError("That pass is not running on this account.")
        for r in rows:
            r.expires_at = min(_aware(r.expires_at), now)
            if _aware(r.starts_at) > now:
                r.starts_at = now
            refund_txn(r.txn_uuid)
        note = f"{len(rows)} pass purchase(s) ended"
    elif what == "tokens":
        rows = db.scalars(select(ChatToken).where(ChatToken.user_id == user.id, ChatToken.used_at.is_(None))).all()
        if not rows:
            raise ValidationError("This account has no unused chat tokens.")
        for r in rows:
            db.delete(r)
        db.flush()
        note = f"{len(rows)} unused chat token(s) removed"
        if refund:                                  # only a pack with nothing used is refunded whole
            for uuid in {r.txn_uuid for r in rows}:
                if not db.scalar(select(func.count(ChatToken.id)).where(ChatToken.txn_uuid == uuid)):
                    refund_txn(uuid)
            note += "; fully unused packs refunded, part-used packs are not"
    elif what == "alias_tokens":
        rows = db.scalars(select(AliasToken).where(AliasToken.user_id == user.id, AliasToken.used_at.is_(None))).all()
        if not rows:
            raise ValidationError("This account has no unused alias tokens.")
        for r in rows:
            refund_txn(r.txn_uuid)
            db.delete(r)
        note = f"{len(rows)} unused alias token(s) removed"
    elif what == "alias":
        row = db.scalar(select(ChatAlias).where(ChatAlias.user_id == user.id))
        if row is None:
            raise ValidationError("This account has no alias.")
        note = f"alias '{row.alias}' removed: chat shows the username again"
        db.delete(row)
    else:
        raise ValidationError("Unknown effect.")
    audit.record(db, admin, "dev.store_revoke", entity_type="user", entity_id=user.id,
                 after={"what": what, "refund": bool(refund), "note": note}, reason=reason[:255])
    db.flush()
    return note


def price_text(cents: int) -> str:
    return fmt(cents)
