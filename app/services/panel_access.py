"""Who may open a Sub Agent's own panel.

An Agent sees its Sub Agents' commission settings. What a Sub Agent keeps to itself (its players and what it earns)
opens only in three steps, in this order:

    1. the Agent asks                      -> the Sub Agent is notified
    2. the Sub Agent allows                -> the Master above is notified and must approve first
    3. the Master approves                 -> the Agent is notified and the panel opens

Nothing shows before step 3. A "no" at step 2 or 3 closes it, and the Sub Agent, the Master or the Admin can
withdraw an approval later. With no Master above (an Admin-made branch) the Admin is the one who approves.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, Forbidden, NotFound
from app.db.base import utcnow
from app.db.models import Notification, PanelAccess, Role, Status, User

CONSENTS_URL = "/agent-panel/consents"
LIVE = ("asked", "allowed", "approved")


def _notify(db: Session, users: list[User], text: str, level: str = "info", link: str = CONSENTS_URL) -> None:
    for user in users:
        db.add(Notification(user_id=user.id, level=level, message=text[:255], link=link))


def master_of(db: Session, user: User) -> User | None:
    """The Master above this account, found by walking up the invite chain (None if there is none)."""
    node, hops = user, 0
    while node is not None and node.parent_id is not None and hops < 8:
        node = db.get(User, node.parent_id)
        hops += 1
        if node is not None and node.role == Role.MASTER:
            return node
    return None


def approvers_for(db: Session, sub: User) -> list[User]:
    master = master_of(db, sub)
    if master is not None and master.status == Status.ACTIVE:
        return [master]
    return list(db.scalars(select(User).where(User.role == Role.ADMIN, User.status == Status.ACTIVE)).all())


def can_approve(db: Session, actor: User, row: PanelAccess) -> bool:
    """The Admin always; a Master only for a Sub Agent that sits under it."""
    if actor.role == Role.ADMIN:
        return True
    if actor.role != Role.MASTER:
        return False
    sub = db.get(User, row.sub_id)
    above = master_of(db, sub) if sub is not None else None
    return above is not None and above.id == actor.id


def _get(db: Session, row_id: int) -> PanelAccess:
    row = db.get(PanelAccess, row_id)
    if row is None:
        raise NotFound("Request not found.")
    return row


def status_for(db: Session, agent_id: int, sub_ids: list[int]) -> dict[int, str]:
    """sub id -> status, for the Sub Agents the Agent has a row for."""
    if not sub_ids:
        return {}
    rows = db.scalars(select(PanelAccess).where(PanelAccess.agent_id == agent_id, PanelAccess.sub_id.in_(sub_ids))).all()
    return {r.sub_id: r.status for r in rows}


def status_by_sub(db: Session, sub_ids: list[int]) -> dict[int, str]:
    """sub id -> status of the request about it (a Sub Agent has one Agent, so one row each)."""
    if not sub_ids:
        return {}
    rows = db.scalars(select(PanelAccess).where(PanelAccess.sub_id.in_(sub_ids)).order_by(PanelAccess.id)).all()
    return {r.sub_id: r.status for r in rows}


def is_open(db: Session, agent_id: int, sub_id: int) -> bool:
    row = db.scalar(select(PanelAccess).where(PanelAccess.agent_id == agent_id, PanelAccess.sub_id == sub_id))
    return row is not None and row.status == "approved"


def ask(db: Session, agent: User, sub: User) -> PanelAccess:
    if agent.role != Role.AGENT or sub.role != Role.SUB_AGENT or sub.parent_id != agent.id:
        raise Forbidden("You can only ask about your own Sub Agents.")
    row = db.scalar(select(PanelAccess).where(PanelAccess.agent_id == agent.id, PanelAccess.sub_id == sub.id))
    if row is not None and row.status in LIVE:
        return row                                     # pressing the button twice changes nothing
    now = utcnow()
    if row is None:
        row = PanelAccess(agent_id=agent.id, sub_id=sub.id, created_at=now)
        db.add(row)
    row.status, row.allowed_at, row.decided_at, row.decided_by_id = "asked", None, None, None
    db.flush()
    _notify(db, [sub], f"{agent.full_name} asks to view your panel. You decide first, then the Master approves.")
    return row


def sub_answer(db: Session, sub: User, row_id: int, allow: bool) -> PanelAccess:
    row = _get(db, row_id)
    if row.sub_id != sub.id:
        raise Forbidden("This request is not about you.")
    if row.status != "asked":
        raise Conflict("This request was already answered.")
    agent = db.get(User, row.agent_id)
    now = utcnow()
    if not allow:
        row.status, row.decided_at, row.decided_by_id = "declined", now, sub.id
        _notify(db, [agent], f"{sub.full_name} declined to open their panel.", "warning")
        return row
    row.status, row.allowed_at = "allowed", now
    _notify(db, approvers_for(db, sub),
            f"{sub.full_name} allowed {agent.full_name} to view their panel. Your approval is needed first.", "warning",
            link="/agent-panel")
    _notify(db, [agent], f"{sub.full_name} allowed it. Waiting for the Master's approval.")
    return row


def approver_answer(db: Session, actor: User, row_id: int, approve: bool) -> PanelAccess:
    row = _get(db, row_id)
    if not can_approve(db, actor, row):
        raise Forbidden("You do not approve this one.")
    if row.status != "allowed":
        raise Conflict("This request is not waiting for approval.")
    agent, sub = db.get(User, row.agent_id), db.get(User, row.sub_id)
    row.status = "approved" if approve else "rejected"
    row.decided_at, row.decided_by_id = utcnow(), actor.id
    word = "approved" if approve else "did not approve"
    _notify(db, [agent], f"The Master {word} your view of {sub.full_name}'s panel.", "success" if approve else "warning",
            link="/agent-panel")
    _notify(db, [sub], f"The Master {word} {agent.full_name} viewing your panel.")
    return row


def revoke(db: Session, actor: User, row_id: int) -> PanelAccess:
    row = _get(db, row_id)
    if not (actor.id == row.sub_id or can_approve(db, actor, row)):
        raise Forbidden("You cannot withdraw this.")
    if row.status not in ("allowed", "approved"):
        raise Conflict("Nothing to withdraw.")
    row.status, row.decided_at, row.decided_by_id = "revoked", utcnow(), actor.id
    _notify(db, [db.get(User, row.agent_id)], "Access to a Sub Agent's panel was withdrawn.", "warning", link="/agent-panel")
    return row


def inbox_for(db: Session, user: User) -> dict[str, list[dict]]:
    """What waits for this person: ``mine`` (a Sub Agent: who asked and who may look) and ``approve`` (a Master or the
    Admin: allowed requests waiting for a yes)."""
    out: dict[str, list[dict]] = {"mine": [], "approve": []}
    if user.role == Role.SUB_AGENT:
        for row in db.scalars(select(PanelAccess).where(PanelAccess.sub_id == user.id)
                              .order_by(PanelAccess.id.desc()).limit(50)).all():
            out["mine"].append({"row": row, "agent": db.get(User, row.agent_id)})
    if user.role in (Role.MASTER, Role.ADMIN):
        for row in db.scalars(select(PanelAccess).where(PanelAccess.status.in_(("allowed", "approved")))
                              .order_by(PanelAccess.id.desc()).limit(200)).all():
            if can_approve(db, user, row):
                out["approve"].append({"row": row, "agent": db.get(User, row.agent_id), "sub": db.get(User, row.sub_id)})
    return out
