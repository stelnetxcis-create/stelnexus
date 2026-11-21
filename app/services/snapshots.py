"""Precomputed report figures, so heavy history scans run in a timer job and not in a web request.

Three figures grow with the business and used to be recomputed on page views:

* ``flows``     all-time loads / cash-outs and the Bola P&L   (``reports.cashflow_flows``)
* ``top``       top earning agents                            (``reports.agent_performance``)
* ``integrity`` the full-ledger reconciliation                (``ledger.verify_integrity``)

``python -m app.cli refresh-reports`` (systemd timer) recomputes and stores them. A page reads the
stored copy. If a figure is missing, or older than its max age (the timer is not running), the
reader computes it inline, stores it and logs a warning: a dead timer degrades to the previous
behaviour, it never serves stale money forever.

Not snapshotted on purpose: agent statements and commission lists. Claims, cancels and redeclared
results change past days, so a stored copy could show wrong money.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.models import ReportSnapshot

log = logging.getLogger("stelnexus.snapshots")

FLOWS, TOP, INTEGRITY = "cashflow.flows", "agents.top", "ledger.integrity"
KINDS = {"flows": FLOWS, "top": TOP, "integrity": INTEGRITY}


@dataclass(frozen=True)
class Snapshot:
    kind: str
    payload: Any
    computed_at: dt.datetime
    duration_ms: int
    inline: bool = False          # computed by the request itself (no usable stored copy)

    @property
    def age_seconds(self) -> float:
        return (utcnow() - self.computed_at).total_seconds()


def _normalise(payload: Any) -> Any:
    """Plain JSON data only (StrEnums -> str, dates -> str), same shape on every database."""
    return json.loads(json.dumps(payload, default=str))


def max_age(kind: str) -> int:
    s = get_settings()
    return s.INTEGRITY_SNAPSHOT_MAX_AGE_SECONDS if kind == INTEGRITY else s.REPORT_SNAPSHOT_MAX_AGE_SECONDS


def read(db: Session, kind: str, scope: str = "") -> Snapshot | None:
    row = db.scalar(select(ReportSnapshot).where(ReportSnapshot.kind == kind, ReportSnapshot.scope == scope))
    if row is None:
        return None
    return Snapshot(kind, row.payload, row.computed_at, int(row.duration_ms or 0))


def write(db: Session, kind: str, payload: Any, *, scope: str = "", duration_ms: int = 0) -> Snapshot:
    """Upsert. Two writers racing on the first insert: the loser hits the unique key, then updates."""
    data, now = _normalise(payload), utcnow()
    row = db.scalar(select(ReportSnapshot).where(ReportSnapshot.kind == kind, ReportSnapshot.scope == scope))
    if row is None:
        try:
            with db.begin_nested():
                db.add(ReportSnapshot(kind=kind, scope=scope, payload=data, computed_at=now,
                                      duration_ms=duration_ms))
            return Snapshot(kind, data, now, duration_ms)
        except IntegrityError:
            row = db.scalar(select(ReportSnapshot).where(ReportSnapshot.kind == kind,
                                                         ReportSnapshot.scope == scope))
    row.payload, row.computed_at, row.duration_ms = data, now, duration_ms
    db.flush()
    return Snapshot(kind, data, now, duration_ms)


def refresh(db: Session, kind: str, compute: Callable[[], Any], *, scope: str = "") -> Snapshot:
    started = time.perf_counter()
    value = compute()
    return write(db, kind, value, scope=scope, duration_ms=int((time.perf_counter() - started) * 1000))


def get(db: Session, kind: str, compute: Callable[[], Any], *, scope: str = "",
        max_age_seconds: int | None = None) -> Snapshot:
    """The stored figure if fresh enough, else compute inline (and store it)."""
    limit = max_age(kind) if max_age_seconds is None else max_age_seconds
    snap = read(db, kind, scope)
    if snap is not None and limit > 0 and snap.age_seconds <= limit:
        return snap
    if snap is not None:
        log.warning("snapshot %s is %.0fs old (max %ss): is the refresh-reports timer running?",
                    kind, snap.age_seconds, limit)
    fresh = refresh(db, kind, compute, scope=scope)
    return Snapshot(fresh.kind, fresh.payload, fresh.computed_at, fresh.duration_ms, inline=True)


# --- the three figures -------------------------------------------------------------------

def _compute(kind: str, db: Session):
    from app.services import ledger, reports
    if kind == FLOWS:
        return lambda: reports.cashflow_flows(db)
    if kind == TOP:
        return lambda: reports.agent_performance(db)
    return lambda: ledger.verify_integrity(db)


def flows(db: Session) -> Snapshot:
    return get(db, FLOWS, _compute(FLOWS, db))


def top_agents(db: Session) -> Snapshot:
    return get(db, TOP, _compute(TOP, db))


def integrity(db: Session) -> Snapshot:
    return get(db, INTEGRITY, _compute(INTEGRITY, db))


def refresh_all(db: Session, only: list[str] | None = None) -> dict[str, Snapshot]:
    """Recompute and store the named figures (default: all). Commits after each so one slow
    figure never holds the others back."""
    out: dict[str, Snapshot] = {}
    for name, kind in KINDS.items():
        if only and name not in only:
            continue
        out[name] = refresh(db, kind, _compute(kind, db))
        db.commit()
    return out
