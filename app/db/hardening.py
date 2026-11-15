"""Database-level guards that must hold even if application code is wrong or compromised.

* ``audit_logs`` is append-only: UPDATE/DELETE/TRUNCATE are refused by triggers.
  The single exception is the FK action ``ON DELETE SET NULL`` on ``actor_id``
  (so removing a user never blocks), which changes nothing else on the row.
Idempotent; safe to call on every start.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

log = logging.getLogger("stelnexus.hardening")
_COLS = ("id", "actor_username", "actor_role", "action", "entity_type", "entity_id",
         '"before"', '"after"', "reason", "ip", "user_agent", "request_id", "created_at")


def _sqlite(conn) -> None:
    changed = " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in _COLS)
    conn.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS audit_logs_no_update BEFORE UPDATE ON audit_logs "
        f"WHEN {changed} BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END")
    conn.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS audit_logs_no_delete BEFORE DELETE ON audit_logs "
        "BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END")


def _postgres(conn) -> None:
    conn.exec_driver_sql("""
CREATE OR REPLACE FUNCTION audit_logs_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'audit_logs is append-only'; END IF;
  IF (to_jsonb(NEW) - 'actor_id') IS DISTINCT FROM (to_jsonb(OLD) - 'actor_id') THEN
    RAISE EXCEPTION 'audit_logs is append-only';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql""")
    conn.exec_driver_sql("""
CREATE OR REPLACE FUNCTION audit_logs_no_truncate() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'audit_logs is append-only'; END $$ LANGUAGE plpgsql""")
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS audit_logs_guard_trg ON audit_logs")
    conn.exec_driver_sql("CREATE TRIGGER audit_logs_guard_trg BEFORE UPDATE OR DELETE ON audit_logs "
                         "FOR EACH ROW EXECUTE FUNCTION audit_logs_guard()")
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS audit_logs_truncate_trg ON audit_logs")
    conn.exec_driver_sql("CREATE TRIGGER audit_logs_truncate_trg BEFORE TRUNCATE ON audit_logs "
                         "FOR EACH STATEMENT EXECUTE FUNCTION audit_logs_no_truncate()")


def install_audit_guards(engine: Engine) -> bool:
    if not inspect(engine).has_table("audit_logs"):
        return False  # tables not created yet; init-db / next start will install
    try:
        with engine.begin() as conn:
            (_sqlite if engine.dialect.name == "sqlite" else _postgres)(conn)
        return True
    except Exception:  # missing privileges etc. -- loud, but never block startup
        log.exception("Could not install audit-log guards; the audit trail is NOT tamper-proof")
        return False
