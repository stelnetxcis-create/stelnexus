#!/usr/bin/env python
"""Index audit. Read-only: it never creates or drops anything.

    python scripts/index_audit.py            # uses DATABASE_URL from .env

Prints (1) single-column indexes that a composite/unique index already covers (drop candidates),
(2) the query plan of the main list queries, flagging full table scans, and (3) on PostgreSQL, the
indexes that have never been used since statistics were last reset.

Read the plans on the PRODUCTION database. On a small dev database the planner scans tables on
purpose, so a "Seq Scan" there means nothing. Index usage (3) only means something after the server
has served real traffic for a week or more; never drop on a fresh stats window.
"""
from __future__ import annotations

import datetime as dt
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore", message="Cannot correctly sort tables")

from sqlalchemy import and_, select, text  # noqa: E402

import app.db.models as m  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import engine  # noqa: E402


PK_ORDER = {"audit trail, newest first"}


def redundant_candidates() -> list[tuple[str, str, str]]:
    out = []
    for t in Base.metadata.sorted_tables:
        idx = [(i.name, tuple(c.name for c in i.columns), i.unique) for i in t.indexes]
        uniq = [tuple(c.name for c in u.columns) for u in t.constraints if u.__class__.__name__ == "UniqueConstraint"]
        for name, cols, unique in idx:
            if unique:
                continue
            for other, ocols, _ in idx:
                if other != name and len(ocols) > len(cols) and ocols[:len(cols)] == cols:
                    out.append((t.name, name, f"leading columns of index {other}"))
                    break
            else:
                for uc in uniq:
                    if len(uc) >= len(cols) and uc[:len(cols)] == cols:
                        out.append((t.name, name, f"leading columns of unique constraint {uc}"))
                        break
    return out


def probe_queries() -> dict[str, object]:
    return {
        "audit trail, newest first": select(m.AuditLog).order_by(m.AuditLog.id.desc()).limit(301),
        "audit trail filtered by entity": select(m.AuditLog).where(m.AuditLog.entity_type == "user")
            .order_by(m.AuditLog.id.desc()).limit(301),
        "tickets of one agent": select(m.Ticket).where(m.Ticket.agent_id == 1)
            .order_by(m.Ticket.id.desc()).limit(301),
        "tickets placed today": select(m.Ticket.id).where(m.Ticket.placed_at >= dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)),
        "one wallet's ledger history": select(m.LedgerEntry).where(m.LedgerEntry.account_id == 1)
            .order_by(m.LedgerEntry.id.desc()).limit(26),
        "commissions of one agent": select(m.Commission).where(and_(m.Commission.agent_id == 1,
            m.Commission.status != m.CommissionStatus.REVERSED)).order_by(m.Commission.id.desc()).limit(300),
        "pending cash requests": select(m.CashRequest).where(m.CashRequest.status == m.RequestStatus.PENDING)
            .order_by(m.CashRequest.id.desc()).limit(200),
        "settled-game history": select(m.Fight).where(m.Fight.status.in_([m.FightStatus.SETTLED, m.FightStatus.VOID]))
            .order_by(m.Fight.declared_at.desc().nulls_last(), m.Fight.id.desc()).limit(151),
        "players of one agent": select(m.Player).where(m.Player.agent_id == 1).order_by(m.Player.id.desc()).limit(201),
    }


def main() -> None:
    print(f"database: {engine.dialect.name}\n")
    print("== single-column indexes covered by a wider index (drop candidates) ==")
    for table, name, why in redundant_candidates():
        print(f"  {table}.{name}: {why}")

    print("\n== query plans ==")
    pg = engine.dialect.name == "postgresql"
    with engine.connect() as conn:
        for label, stmt in probe_queries().items():
            sql = str(stmt.compile(engine, compile_kwargs={"literal_binds": True}))
            plan = conn.execute(text(("EXPLAIN " if pg else "EXPLAIN QUERY PLAN ") + sql)).fetchall()
            lines = [str(r[0] if pg else r[-1]) for r in plan]
            # "newest first" with no filter is a walk down the primary key: a scan by design, stopped by LIMIT
            scan = label not in PK_ORDER and any(
                ("Seq Scan" in ln) if pg else (ln.startswith("SCAN") and "USING" not in ln) for ln in lines)
            print(f"\n-- {label}{'   <-- FULL SCAN' if scan else ''}")
            for ln in lines[:6]:
                print("   " + ln)

        if pg:
            print("\n== indexes never used since statistics reset (non-unique, not primary key) ==")
            rows = conn.execute(text("""
                SELECT s.relname, s.indexrelname, s.idx_scan, pg_size_pretty(pg_relation_size(s.indexrelid))
                FROM pg_stat_user_indexes s JOIN pg_index i ON i.indexrelid = s.indexrelid
                WHERE s.idx_scan = 0 AND NOT i.indisunique AND NOT i.indisprimary
                ORDER BY pg_relation_size(s.indexrelid) DESC""")).fetchall()
            for r in rows:
                print(f"  {r[0]}.{r[1]}  scans={r[2]}  size={r[3]}")
            reset = conn.execute(text("SELECT stats_reset FROM pg_stat_database WHERE datname = current_database()")).scalar()
            print(f"  (statistics since: {reset})")
        else:
            print("\n(index usage statistics need PostgreSQL: pg_stat_user_indexes)")


if __name__ == "__main__":
    main()
