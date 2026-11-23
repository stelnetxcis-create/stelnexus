"""Admin Panel account tools: edit an account's details, or remove it.

Who may use them is a permission (``accounts.edit`` / ``accounts.remove``): the Admin always, a
Pawn only when the Admin ticked the grant. The rules below hold for everyone, Pawn or not:

* the Admin account can never be edited or removed here;
* only the Admin may touch a Pawn (otherwise a Pawn could remove the Pawn that outranks it);
* you cannot remove yourself.

"Remove" is a **close**, never a delete: tickets, ledger entries and audit rows point at the
account and must stay readable. A closed account cannot sign in, its sessions are revoked, it
disappears from the default lists and its username stays reserved. To keep the books honest it is
refused while the account still has credit, an open bet, a pending cash request, an active
downline or active players.
"""
from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.pagination import clamp_limit, clamp_offset
from app.core import audit
from app.core.errors import Conflict, Forbidden, NotFound, ValidationError
from app.core.rbac import Perm, has_perm
from app.core.security import USERNAME_RE, mask, pii
from app.db.models import (
    BolaTicket,
    CashRequest,
    Player,
    RequestStatus,
    Role,
    Status,
    Ticket,
    TicketStatus,
    User,
)
from app.services import accounts, agents

GCASH_RE = re.compile(r"^09\d{9}$")


# ----------------------------------------------------------------------------- guards

def _user_target(db: Session, actor: User, user_id: int, perm: Perm) -> User:
    if not has_perm(actor, perm):
        raise Forbidden("You are not allowed to do that.")
    target = db.get(User, user_id)
    # The Admin answers like a missing row, so its existence cannot be probed.
    if target is None or target.role in (Role.ADMIN, Role.BISHOP, Role.TELLER):
        raise NotFound("Account not found.")
    if target.role == Role.PAWN and not actor.is_admin_tier:
        raise Forbidden("Only the Admin can change a Pawn.")
    return target


def _player_target(db: Session, actor: User, player_id: int, perm: Perm) -> Player:
    if not has_perm(actor, perm):
        raise Forbidden("You are not allowed to do that.")
    target = db.get(Player, player_id)
    if target is None:
        raise NotFound("Player not found.")
    return target


def _name_taken(db: Session, username: str, *, user_id: int | None = None, player_id: int | None = None) -> bool:
    low = username.lower()
    q_user = select(User.id).where(func.lower(User.username) == low)
    q_player = select(Player.id).where(func.lower(Player.username) == low)
    if user_id:
        q_user = q_user.where(User.id != user_id)
    if player_id:
        q_player = q_player.where(Player.id != player_id)
    return bool(db.scalar(q_user) or db.scalar(q_player))


def _clean_name(value: str | None) -> str:
    value = (value or "").strip()
    if not value or len(value) > 120:
        raise ValidationError("Full name is required (max 120 characters).")
    return value


def _clean_username(db: Session, value: str | None, *, user_id=None, player_id=None) -> str:
    value = (value or "").strip()
    if not USERNAME_RE.match(value):
        raise ValidationError("Username must be 3-32 characters: letters, digits, dot, dash, underscore.")
    if _name_taken(db, value, user_id=user_id, player_id=player_id):
        raise Conflict("That username is already taken.")
    return value


# ----------------------------------------------------------------------------- edit

def edit_user(db: Session, actor: User, user_id: int, *, username: str | None = None,
              full_name: str | None = None, contact: str | None = None,
              reason: str | None = None) -> User:
    target = _user_target(db, actor, user_id, Perm.ACCOUNTS_EDIT)
    if target.status == Status.CLOSED:
        raise Conflict("That account was removed. Nothing on it can be changed.")
    before = {"username": target.username, "full_name": target.full_name, "contact": target.contact}
    if username is not None and username.strip() != target.username:
        target.username = _clean_username(db, username, user_id=target.id)
        agents.revoke_sessions(db, target)        # the old name is no longer who they are
    if full_name is not None:
        target.full_name = _clean_name(full_name)
    if contact is not None:
        target.contact = contact.strip()[:32] or None
    db.flush()
    after = {"username": target.username, "full_name": target.full_name, "contact": target.contact}
    if after != before:
        audit.record(db, actor, "account.edit", entity_type="user", entity_id=target.id,
                     before=before, after=after, reason=reason)
    return target


def edit_player(db: Session, actor: User, player_id: int, *, username: str | None = None,
                full_name: str | None = None, gcash_number: str | None = None,
                address: str | None = None, notes: str | None = None,
                reason: str | None = None) -> Player:
    target = _player_target(db, actor, player_id, Perm.ACCOUNTS_EDIT)
    if target.status == Status.CLOSED:
        raise Conflict("That player was removed. Nothing on it can be changed.")
    before = {"username": target.username, "full_name": target.full_name,
              "gcash": mask(pii.decrypt(target.gcash_enc) or ""), "notes": target.notes}
    if username is not None and username.strip() != (target.username or ""):
        if target.username is None:
            raise ValidationError("A card player has no username.")
        target.username = _clean_username(db, username, player_id=target.id)
    if full_name is not None:
        target.full_name = _clean_name(full_name)
    if gcash_number is not None and gcash_number.strip():
        if not GCASH_RE.match(gcash_number.strip()):
            raise ValidationError("GCash number must be 11 digits starting with 09.")
        target.gcash_enc = pii.encrypt(gcash_number.strip())
    if address is not None:
        target.address_enc = pii.encrypt(address.strip()[:255]) if address.strip() else None
    if notes is not None:
        target.notes = notes.strip()[:500] or None
    db.flush()
    after = {"username": target.username, "full_name": target.full_name,
             "gcash": mask(pii.decrypt(target.gcash_enc) or ""), "notes": target.notes}
    if after != before:
        audit.record(db, actor, "account.edit", entity_type="player", entity_id=target.id,
                     before=before, after=after, reason=reason)
    return target


# ----------------------------------------------------------------------------- remove

def _need_reason(reason: str | None) -> str:
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValidationError("A written reason (at least 5 characters) is required to remove an account.")
    return reason[:255]


def removal_blockers_user(db: Session, target: User) -> list[str]:
    out: list[str] = []
    kids = db.scalar(select(func.count(User.id)).where(User.parent_id == target.id,
                                                       User.status != Status.CLOSED)) or 0
    if kids:
        out.append(f"{kids} active account(s) report to it. Remove or move them first.")
    pl = db.scalar(select(func.count(Player.id)).where(Player.agent_id == target.id,
                                                       Player.status != Status.CLOSED)) or 0
    if pl:
        out.append(f"{pl} active player(s) belong to it. Remove them first.")
    if target.has_wallet:
        for label, acct in (("credit", accounts.wallet_for_user(db, target)),
                            ("commission", accounts.commission_for_user(db, target))):
            if acct.balance_cents:
                out.append(f"It still holds {label}. Cash it out or transfer it first.")
    pending = db.scalar(select(func.count(CashRequest.id)).where(
        (CashRequest.agent_id == target.id) | (CashRequest.requested_by_id == target.id),
        CashRequest.status == RequestStatus.PENDING)) or 0
    if pending:
        out.append(f"{pending} cash request(s) are still pending.")
    return out


def removal_blockers_player(db: Session, target: Player) -> list[str]:
    out: list[str] = []
    acct = accounts.wallet_for_player(db, target)
    if acct.balance_cents:
        out.append("The wallet still holds credit. Cash it out first.")
    open_sabong = db.scalar(select(func.count(Ticket.id)).where(
        Ticket.player_id == target.id, Ticket.status == TicketStatus.ACTIVE)) or 0
    open_bola = db.scalar(select(func.count(BolaTicket.id)).where(
        BolaTicket.player_id == target.id, BolaTicket.status == TicketStatus.ACTIVE)) or 0
    if open_sabong or open_bola:
        out.append(f"{open_sabong + open_bola} bet(s) are still open. Wait for the result or cancel them.")
    pending = db.scalar(select(func.count(CashRequest.id)).where(
        CashRequest.player_id == target.id, CashRequest.status == RequestStatus.PENDING)) or 0
    if pending:
        out.append(f"{pending} cash request(s) are still pending.")
    return out


def remove_user(db: Session, actor: User, user_id: int, *, reason: str) -> User:
    target = _user_target(db, actor, user_id, Perm.ACCOUNTS_REMOVE)
    if target.id == actor.id:
        raise ValidationError("You cannot remove your own account.")
    reason = _need_reason(reason)
    if target.status == Status.CLOSED:
        raise Conflict("That account is already removed.")
    blockers = removal_blockers_user(db, target)
    if blockers:
        raise Conflict("Cannot remove " + target.username + ": " + " ".join(blockers))
    before = {"status": target.status, "username": target.username}
    target.status = Status.CLOSED
    agents.revoke_sessions(db, target)
    db.flush()
    audit.record(db, actor, "account.remove", entity_type="user", entity_id=target.id,
                 before=before, after={"status": "closed", "role": target.role.value}, reason=reason)
    return target


def remove_player(db: Session, actor: User, player_id: int, *, reason: str) -> Player:
    target = _player_target(db, actor, player_id, Perm.ACCOUNTS_REMOVE)
    reason = _need_reason(reason)
    if target.status == Status.CLOSED:
        raise Conflict("That player is already removed.")
    blockers = removal_blockers_player(db, target)
    if blockers:
        raise Conflict("Cannot remove " + (target.username or target.card_number or str(target.id))
                       + ": " + " ".join(blockers))
    before = {"status": target.status, "username": target.username}
    target.status = Status.CLOSED
    db.flush()
    audit.record(db, actor, "account.remove", entity_type="player", entity_id=target.id,
                 before=before, after={"status": "closed", "kind": target.kind.value}, reason=reason)
    return target


# ----------------------------------------------------------------------------- listing

def list_accounts(db: Session, *, search: str | None = None, kind: str = "all",
                  include_removed: bool = False, limit: int = 100,
                  user_offset: int = 0, player_offset: int = 0) -> dict:
    """One page of staff / agent accounts and one page of players (each has its own offset)."""
    users: list[User] = []
    plist: list[Player] = []
    term = f"%{search.strip()[:60]}%" if search and search.strip() else None
    if kind in ("all", "users"):
        q = select(User).where(User.role.notin_([Role.ADMIN, Role.BISHOP, Role.TELLER]))
        if not include_removed:
            q = q.where(User.status != Status.CLOSED)
        if term:
            q = q.where(User.username.ilike(term) | User.full_name.ilike(term))
        users = list(db.scalars(q.order_by(User.id.desc()).limit(clamp_limit(limit, 501))
                                .offset(clamp_offset(user_offset))).all())
    if kind in ("all", "players"):
        q = select(Player)
        if not include_removed:
            q = q.where(Player.status != Status.CLOSED)
        if term:
            q = q.where(Player.username.ilike(term) | Player.full_name.ilike(term)
                        | Player.card_number.ilike(term))
        plist = list(db.scalars(q.order_by(Player.id.desc()).limit(clamp_limit(limit, 501))
                                .offset(clamp_offset(player_offset))).all())
    return {"users": users, "players": plist}
