"""Small, idempotent schema upgrade for databases created by an older StelNexus.

The project has no Alembic history, so ``Base.metadata.create_all`` only ever adds
*tables*. This module covers what create_all cannot:

* ``ALTER TABLE ... ADD COLUMN`` for columns added to existing tables;
* replacing CHECK constraints whose allowed values changed
  (``users.role`` gained ``declarator`` and then became admin/master/cavo/kubrador;
  ``tickets.side`` gained ``draw``);
* renaming the old tiers in existing rows: gold -> cavo, sub -> kubrador (users, referral codes,
  commission lines). ``audit_logs`` is append-only and is deliberately left as it was written;
* merging the tiers: cavo -> agent and kubrador -> sub_agent (users, referral codes, commission
  lines; amounts are untouched);
* relabelling the house account and the online-payments clearing account;
* ``users.role`` also gained ``pawn``; new columns ``users.grants``, ``events.location_id`` and
  ``cash_requests.target`` are added, and the ``locations`` table is created;
* ``bola_tickets.player_id`` becomes nullable and ``bola_tickets.user_id`` is added: the number game now
  lets the logged-in account bet from its own credit (like the fight game) instead of forcing a player;
* one-time rollout of the new payout limits (minimum odds 1.50x -> 1.30x with cut relief, maximum bet
  PHP 50,000 -> PHP 10,000): a stored value that still equals the OLD default is moved to the new default,
  a value an admin chose is left alone, and it never runs twice (see ``_roll_out_payout_limits``);
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
    # Master Agent -> Agent -> Sub Agent ladder (the Master keeps its stored value 'master').
    ("users", "role_valid",
     "role in ('admin','master','cavo','kubrador','teller','declarator','pawn')",
     "role in ('admin','master','cavo','kubrador','teller','declarator','pawn','agent','sub_agent')",
     "sub_agent"),
    # The Bishop: a deputy admin below the King.
    ("users", "role_valid",
     "role in ('admin','master','cavo','kubrador','teller','declarator','pawn','agent','sub_agent')",
     "role in ('admin','master','cavo','kubrador','teller','declarator','pawn','agent','sub_agent','bishop')",
     "bishop"),
    # ... and for databases created from the current models (no retired tier names in their CHECK text).
    ("users", "role_valid",
     "role in ('admin','master','teller','declarator','pawn','agent','sub_agent')",
     "role in ('admin','master','teller','declarator','pawn','agent','sub_agent','bishop')",
     "bishop"),
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
        previous_isolation = dbapi.isolation_level
        dbapi.isolation_level = None  # manual transaction control (restored below: the connection goes back to the pool)
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
        # A pooled connection left in manual-commit mode silently turns every later request on it into autocommit:
        # a failed request then cannot roll back and leaves half-written data behind.
        dbapi.isolation_level = previous_isolation
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


# (table, column) pairs that used to be NOT NULL and must now accept NULL.
RELAX_NOT_NULL = (("bola_tickets", "player_id"),)


def _relax_not_null(engine: Engine) -> list[str]:
    """Drop NOT NULL on the columns in RELAX_NOT_NULL (``create_all`` never alters an existing table).
    SQLite cannot ALTER a column, so its stored CREATE TABLE text is edited under writable_schema, the same
    documented route the CHECK upgrade uses. Idempotent; never touches rows."""
    import re
    changed: list[str] = []
    inspector = inspect(engine)
    pending = []
    for table, column in RELAX_NOT_NULL:
        if not inspector.has_table(table):
            continue
        info = {c["name"]: c for c in inspector.get_columns(table)}
        if column in info and not info[column]["nullable"]:
            pending.append((table, column))
    if not pending:
        return changed
    if engine.dialect.name == "sqlite":
        raw = engine.raw_connection()
        try:
            dbapi = raw.driver_connection
            previous_isolation = dbapi.isolation_level
            dbapi.isolation_level = None
            cur = dbapi.cursor()
            for table, column in pending:
                row = cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                if not row or not row[0]:
                    continue
                new_sql, n = re.subn(rf'(["`]?\b{column}["`]?\s+INTEGER)\s+NOT\s+NULL', r"\1", row[0], count=1)
                if not n:
                    continue
                cur.execute("BEGIN IMMEDIATE")
                version = cur.execute("PRAGMA schema_version").fetchone()[0]
                cur.execute("PRAGMA writable_schema=ON")
                cur.execute("UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?", (new_sql, table))
                cur.execute(f"PRAGMA schema_version={version + 1}")
                cur.execute("PRAGMA writable_schema=OFF")
                cur.execute("COMMIT")
                changed.append(f"{table}.{column}: NOT NULL dropped")
            if changed:
                ok = cur.execute("PRAGMA integrity_check").fetchone()[0]
                if ok != "ok":
                    log.error("SQLite integrity_check after NOT NULL upgrade: %s", ok)
            cur.close()
        finally:
            dbapi.isolation_level = previous_isolation
            raw.close()
    else:
        with engine.begin() as conn:
            for table, column in pending:
                conn.exec_driver_sql(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" DROP NOT NULL')
                changed.append(f"{table}.{column}: NOT NULL dropped")
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


# Cavo and Kubrador were merged into Agent and Sub Agent (the cut rules carry over unchanged:
# Cavo's Volume % / SOP % -> Agent's, Kubrador's SOP % -> Sub Agent's).
TIER_MERGES = (("cavo", "agent"), ("kubrador", "sub_agent"))


def _merge_tiers(engine: Engine) -> list[str]:
    """cavo -> agent, kubrador -> sub_agent in users, referral codes and both commission tables.
    Raw SQL (the ORM no longer hands out the retired roles). Idempotent; changes no amounts.

    A Cavo that was recruited under one of the short-lived Agent / Sub Agent accounts of the
    previous ladder now sits under an Agent or Sub Agent; it still earns correctly (commission looks
    for the nearest Agent and Sub Agent above the seller) but is listed in the report so the
    operator can move it under a Master Agent if wanted."""
    inspector = inspect(engine)
    changed: list[str] = []
    if not inspector.has_table("users"):
        return changed
    with engine.begin() as conn:
        odd = conn.execute(text(
            "SELECT c.username, p.role FROM users c JOIN users p ON p.id = c.parent_id "
            "WHERE c.role IN ('cavo','kubrador') AND p.role IN ('agent','sub_agent')")).all()
        for table, column in (("users", "role"), ("referral_codes", "role"),
                              ("commissions", "tier"), ("bola_commissions", "tier")):
            if not inspector.has_table(table):
                continue
            for old, new in TIER_MERGES:
                result = conn.execute(text(f'UPDATE "{table}" SET "{column}" = :new WHERE "{column}" = :old'),
                                      {"new": new, "old": old})
                if result.rowcount:
                    changed.append(f"{table}.{column}: {old}->{new} x{result.rowcount}")
        for name, parent_role in odd:
            changed.append(f"review: {name} (was under a {parent_role}) now sits under an Agent / Sub Agent")
    return changed


def _retire_tellers(engine: Engine) -> list[str]:
    """Close every teller login. Idempotent; never deletes anything, so history stays intact."""
    inspector = inspect(engine)
    if not inspector.has_table("users"):
        return []
    changed: list[str] = []
    with engine.begin() as conn:
        result = conn.execute(text("UPDATE users SET status = 'closed' "
                                   "WHERE role = 'teller' AND gcash_enc IS NULL AND COALESCE(status, '') <> 'closed'"))
        if result.rowcount:
            changed.append(f"users: closed {result.rowcount} teller account(s)")
            if inspector.has_table("sessions"):
                conn.execute(text("UPDATE sessions SET revoked_at = CURRENT_TIMESTAMP "
                                  "WHERE revoked_at IS NULL AND user_id IN "
                                  "(SELECT id FROM users WHERE role = 'teller' AND gcash_enc IS NULL)"))
        if inspector.has_table("stations"):
            conn.execute(text("UPDATE stations SET is_active = 0 WHERE is_active <> 0")
                         if engine.dialect.name == "sqlite"
                         else text("UPDATE stations SET is_active = false WHERE is_active"))
    return changed


def _seed_gcash_pool(engine: Engine) -> list[str]:
    """Every active teller that has a personal GCash number but no pool entry gets one, so an
    existing install keeps routing to the same numbers after the pool feature arrives.

    Plain SQL on purpose: this runs before the other upgrades may have finished on an old
    database, so it must not depend on the full ORM column set of ``users``."""
    inspector = inspect(engine)
    if not (inspector.has_table("users") and inspector.has_table("gcash_accounts")):
        return []
    user_cols = {c["name"] for c in inspector.get_columns("users")}
    if not {"id", "role", "status", "gcash_enc", "full_name"} <= user_cols:
        return []
    from app.core.security import pii
    from app.db.models import GcashAccount
    from app.services import gcash
    seeded: list[str] = []
    try:
        with engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT u.id, u.username, u.full_name, u.gcash_enc FROM users u "
                "WHERE u.role = 'teller' AND u.status = 'active' AND u.gcash_enc IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM gcash_accounts g WHERE g.owner_id = u.id)")).all()
            order = int(conn.execute(text("SELECT COALESCE(MAX(sort_order), 0) FROM gcash_accounts")).scalar() or 0)
            for user_id, username, full_name, enc in rows:
                try:
                    number = gcash.normalize_number(pii.decrypt(enc) or "")
                except Exception:  # noqa: BLE001 - an unreadable legacy value must not block startup
                    log.warning("Could not seed a GCash number for %s", username)
                    continue
                digest = gcash._hash(number)
                if conn.execute(text("SELECT 1 FROM gcash_accounts WHERE number_hash = :h"), {"h": digest}).first():
                    continue
                order += 1
                conn.execute(GcashAccount.__table__.insert().values(
                    owner_id=user_id, label=f"{full_name or username} \u00b7 main", number_enc=pii.encrypt(number),
                    number_hash=digest, is_active=True, sort_order=order))
                seeded.append(f"gcash_accounts: seeded {username}")
    except Exception:  # noqa: BLE001 - never block startup; the pool can always be filled by hand
        log.exception("GCash pool seeding skipped")
    return seeded


# stored value still equal to the old default -> new default (only ever applied once)
PAYOUT_LIMIT_ROLLOUT = (("payout.min_odds_bps", "15000", "13000"),
                        ("payout.max_stake_cents", "5000000", "1000000"))


def _roll_out_payout_limits(engine: Engine) -> list[str]:
    """Settings are stored in ``app_settings`` and override the code defaults, so changing a default alone would not
    reach an existing database. The first start after this upgrade moves a stored value that is still the old
    default to the new one. ``payout.min_odds_mode`` is the marker: once its row exists this never runs again, so a
    value an admin sets later (even the old one) is never overwritten."""
    if not inspect(engine).has_table("app_settings"):
        return []
    done: list[str] = []
    with engine.begin() as conn:
        if conn.execute(text("SELECT 1 FROM app_settings WHERE key = 'payout.min_odds_mode'")).first():
            return []
        for key, old, new in PAYOUT_LIMIT_ROLLOUT:
            res = conn.execute(text("UPDATE app_settings SET value = :new WHERE key = :key AND value = :old"),
                               {"key": key, "old": old, "new": new})
            if res.rowcount:
                done.append(f"{key}: {old} -> {new}")
        conn.execute(text("INSERT INTO app_settings (key, value, updated_at) "
                          "VALUES ('payout.min_odds_mode', 'relief_all', CURRENT_TIMESTAMP)"))
    return done


def upgrade(engine: Engine) -> dict[str, list[str]]:
    import app.db.models  # noqa: F401  (register tables)
    Base.metadata.create_all(engine)
    report = {
        "columns": _add_missing_columns(engine),
        "checks": (_upgrade_checks_sqlite if engine.dialect.name == "sqlite"
                   else _upgrade_checks_postgres)(engine),
    }
    report["nullable"] = _relax_not_null(engine)
    report["roles"] = _rename_roles(engine)
    report["tiers"] = _merge_tiers(engine)
    report["tellers"] = _retire_tellers(engine)
    report["gcash_pool"] = _seed_gcash_pool(engine)
    report["payout_limits"] = _roll_out_payout_limits(engine)
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
