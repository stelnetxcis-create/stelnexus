"""Small, idempotent schema upgrade for databases created by an older StelNexus.

The project has no Alembic history, so ``Base.metadata.create_all`` only ever adds
*tables*. This module covers what create_all cannot:

* ``ALTER TABLE ... ADD COLUMN`` for columns added to existing tables;
* replacing CHECK constraints whose allowed values changed
  (``users.role`` gained ``declarator`` and then became admin/master/cavo/kubrador;
  ``tickets.side`` gained ``draw``);
* renaming the old tiers in existing rows: gold -> cavo, sub -> kubrador (users, referral codes,
  commission lines). ``audit_logs`` is append-only and is deliberately left as it was written;
* relabelling the house account and the online-payments clearing account;
* ``users.role`` also gained ``pawn``; new columns ``users.grants``, ``events.location_id`` and
  ``cash_requests.target`` are added, and the ``locations`` table is created;
* retiring the teller role: the platform is online-only, so existing teller accounts are
  closed and their sessions revoked (rows, tickets and ledger history are kept).

Safe to run on every start. It never drops data.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.db.base import Base

log = logging.getLogger("stelnexus.migrate")

# (table, constraint name suffix, old expression, new expression, marker found only in the new one)
CHECK_UPGRADES = (
    ("users", "role_valid",
     "role in ('admin','master','gold','sub','teller')",
     "role in ('admin','master','gold','sub','teller','declarator')", "declarator"),
    ("users", "role_valid",
     "role in ('admin','master','gold','sub','teller','declarator')",
     "role in ('admin','master','cavo','kubrador','teller','declarator')", "kubrador"),
    ("users", "role_valid",
     "role in ('admin','master','cavo','kubrador','teller','declarator')",
     "role in ('admin','master','cavo','kubrador','teller','declarator','pawn')", "pawn"),
    ("tickets", "side_valid",
     "side in ('meron','wala')",
     "side in ('meron','wala','draw')", "draw"),
)

# Old tier name -> new tier name.
ROLE_RENAMES = (("gold", "cavo"), ("sub", "kubrador"))


def _add_missing_columns(engine: Engine) -> list[str]:
    added: list[str] = []
    inspector = inspect(engine)
    dialect = engine.dialect
    for table in Base.metadata.tables.values():
        if not inspector.has_table(table.name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            ddl_type = column.type.compile(dialect=dialect)
            parts = [f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}']
            if column.server_default is not None:
                parts.append(f"DEFAULT {column.server_default.arg!r}" if isinstance(
                    column.server_default.arg, str) else "")
            if not column.nullable:
                if column.server_default is None:
                    log.warning("Skipping NOT NULL column %s.%s without a default", table.name, column.name)
                    continue
                parts.append("NOT NULL")
            with engine.begin() as conn:
                conn.exec_driver_sql(" ".join(p for p in parts if p))
                if dialect.name == "postgresql":
                    for fk in column.foreign_keys:
                        conn.exec_driver_sql(
                            f'ALTER TABLE "{table.name}" ADD FOREIGN KEY ("{column.name}") '
                            f'REFERENCES "{fk.column.table.name}" ("{fk.column.name}") '
                            f"ON DELETE SET NULL")
                if column.index:
                    conn.exec_driver_sql(
                        f'CREATE INDEX IF NOT EXISTS "ix_{table.name}_{column.name}" '
                        f'ON "{table.name}" ("{column.name}")')
            added.append(f"{table.name}.{column.name}")
    return added


def _upgrade_checks_sqlite(engine: Engine) -> list[str]:
    """SQLite cannot ALTER a CHECK. The documented safe route for a looser CHECK is to
    edit the stored CREATE TABLE text under PRAGMA writable_schema."""
    changed: list[str] = []
    raw = engine.raw_connection()
    try:
        dbapi = raw.driver_connection
        dbapi.isolation_level = None  # manual transaction control
        cur = dbapi.cursor()
        for table, name, old, new, _marker in CHECK_UPGRADES:
            row = cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                              (table,)).fetchone()
            if not row or not row[0] or old not in row[0]:
                continue
            new_sql = row[0].replace(old, new)
            cur.execute("BEGIN IMMEDIATE")
            version = cur.execute("PRAGMA schema_version").fetchone()[0]
            cur.execute("PRAGMA writable_schema=ON")
            cur.execute("UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?", (new_sql, table))
            cur.execute(f"PRAGMA schema_version={version + 1}")
            cur.execute("PRAGMA writable_schema=OFF")
            cur.execute("COMMIT")
            changed.append(f"{table}.{name}")
        if changed:
            ok = cur.execute("PRAGMA integrity_check").fetchone()[0]
            if ok != "ok":
                log.error("SQLite integrity_check after CHECK upgrade: %s", ok)
        cur.close()
    finally:
        raw.close()
    return changed


def _upgrade_checks_postgres(engine: Engine) -> list[str]:
    changed: list[str] = []
    inspector = inspect(engine)
    for table, name, old, new, marker in CHECK_UPGRADES:
        if not inspector.has_table(table):
            continue
        constraint = f"ck_{table}_{name}"
        with engine.begin() as conn:
            current = conn.execute(text(
                "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid WHERE t.relname = :t AND c.conname = :n"),
                {"t": table, "n": constraint}).scalar()
            if current is None or marker in current:
                continue
            conn.exec_driver_sql(f'ALTER TABLE "{table}" DROP CONSTRAINT "{constraint}"')
            conn.exec_driver_sql(f'ALTER TABLE "{table}" ADD CONSTRAINT "{constraint}" CHECK ({new})')
            changed.append(f"{table}.{name}")
    return changed


def _rename_roles(engine: Engine) -> list[str]:
    """gold -> cavo, sub -> kubrador. Runs after the CHECK is widened, with raw SQL so the ORM's
    enum validation never sees the old values. Idempotent."""
    changed: list[str] = []
    inspector = inspect(engine)
    targets = (("users", "role"), ("referral_codes", "role"), ("commissions", "tier"))
    with engine.begin() as conn:
        for table, column in targets:
            if not inspector.has_table(table):
                continue
            for old, new in ROLE_RENAMES:
                result = conn.execute(text(f'UPDATE "{table}" SET "{column}" = :new WHERE "{column}" = :old'),
                                      {"new": new, "old": old})
                if result.rowcount:
                    changed.append(f"{table}.{column}: {old}->{new} x{result.rowcount}")
    return changed


def _retire_tellers(engine: Engine) -> list[str]:
    """Close every teller login. Idempotent; never deletes anything, so history stays intact."""
    inspector = inspect(engine)
    if not inspector.has_table("users"):
        return []
    changed: list[str] = []
    with engine.begin() as conn:
        result = conn.execute(text("UPDATE users SET status = 'closed' "
                                   "WHERE role = 'teller' AND COALESCE(status, '') <> 'closed'"))
        if result.rowcount:
            changed.append(f"users: closed {result.rowcount} teller account(s)")
            if inspector.has_table("sessions"):
                conn.execute(text("UPDATE sessions SET revoked_at = CURRENT_TIMESTAMP "
                                  "WHERE revoked_at IS NULL AND user_id IN "
                                  "(SELECT id FROM users WHERE role = 'teller')"))
        if inspector.has_table("stations"):
            conn.execute(text("UPDATE stations SET is_active = 0 WHERE is_active <> 0")
                         if engine.dialect.name == "sqlite"
                         else text("UPDATE stations SET is_active = false WHERE is_active"))
    return changed


def upgrade(engine: Engine) -> dict[str, list[str]]:
    import app.db.models  # noqa: F401  (register tables)
    Base.metadata.create_all(engine)
    report = {
        "columns": _add_missing_columns(engine),
        "checks": (_upgrade_checks_sqlite if engine.dialect.name == "sqlite"
                   else _upgrade_checks_postgres)(engine),
    }
    report["roles"] = _rename_roles(engine)
    report["tellers"] = _retire_tellers(engine)
    inspector = inspect(engine)
    if inspector.has_table("accounts"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE accounts SET label = 'House Revenue (Tong)' "
                              "WHERE code = 'sys:house' AND label LIKE '%Plasada%'"))
            conn.execute(text("UPDATE accounts SET label = 'Online Payments Clearing (GCash / Bank)' "
                              "WHERE code = 'sys:gcash' AND label = 'GCash Clearing'"))
            conn.execute(text("UPDATE accounts SET label = 'Legacy Cash (retired, pre-online)' "
                              "WHERE code = 'sys:cash' AND label = 'Central Cash On Hand'"))
    if any(report.values()):
        log.info("Schema upgraded: %s", report)
    return report
