"""Small, idempotent schema upgrade for databases created by an older StelNexus.

The project has no Alembic history, so ``Base.metadata.create_all`` only ever adds
*tables*. This module covers what create_all cannot:

* ``ALTER TABLE ... ADD COLUMN`` for columns added to existing tables;
* replacing two CHECK constraints whose allowed values grew
  (``users.role`` gained ``declarator``; ``tickets.side`` gained ``draw``);
* relabelling the house account.

Safe to run on every start. It never drops data.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.db.base import Base

log = logging.getLogger("stelnexus.migrate")

# (table, constraint name suffix, old expression, new expression)
CHECK_UPGRADES = (
    ("users", "role_valid",
     "role in ('admin','master','gold','sub','teller')",
     "role in ('admin','master','gold','sub','teller','declarator')"),
    ("tickets", "side_valid",
     "side in ('meron','wala')",
     "side in ('meron','wala','draw')"),
)


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
        for table, name, old, new in CHECK_UPGRADES:
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
    for table, name, old, new in CHECK_UPGRADES:
        if not inspector.has_table(table):
            continue
        constraint = f"ck_{table}_{name}"
        with engine.begin() as conn:
            current = conn.execute(text(
                "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid WHERE t.relname = :t AND c.conname = :n"),
                {"t": table, "n": constraint}).scalar()
            if current is None or "draw" in current or "declarator" in current:
                continue
            conn.exec_driver_sql(f'ALTER TABLE "{table}" DROP CONSTRAINT "{constraint}"')
            conn.exec_driver_sql(f'ALTER TABLE "{table}" ADD CONSTRAINT "{constraint}" CHECK ({new})')
            changed.append(f"{table}.{name}")
    return changed


def upgrade(engine: Engine) -> dict[str, list[str]]:
    import app.db.models  # noqa: F401  (register tables)
    Base.metadata.create_all(engine)
    report = {
        "columns": _add_missing_columns(engine),
        "checks": (_upgrade_checks_sqlite if engine.dialect.name == "sqlite"
                   else _upgrade_checks_postgres)(engine),
    }
    inspector = inspect(engine)
    if inspector.has_table("accounts"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE accounts SET label = 'House Revenue (Tong)' "
                              "WHERE code = 'sys:house' AND label LIKE '%Plasada%'"))
    if any(report.values()):
        log.info("Schema upgraded: %s", report)
    return report
