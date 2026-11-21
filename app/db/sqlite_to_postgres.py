"""Copy a StelNexus SQLite database into an EMPTY PostgreSQL database.

    python -m app.cli sqlite-to-postgres --source sqlite:///./dev.db --target postgresql+psycopg://u:p@host/db
    python -m app.cli sqlite-to-postgres ... --apply          # without --apply nothing is written

How it stays correct
* The source is opened read-only; it is never modified.
* The target must be empty. The schema is created by the app's own ``upgrade`` (so it is exactly
  what a fresh install gets), the audit-log guards are installed, then rows are copied with the same
  SQLAlchemy ``Table`` objects, so every type (UTC datetimes, enums, JSON, booleans) round-trips.
* Tables are copied parents-first. A foreign key that points at the same table or at a table copied
  later (users.parent_id, ledger_txns.reverses_txn_id, ...) is inserted as NULL and filled in by a
  second pass, so no row ever references one that does not exist yet.
* Everything happens in ONE transaction. Row counts of every table, the ledger totals, and the
  ledger's own reconciliation (``ledger.verify_integrity``) are checked before COMMIT. Any mismatch
  rolls back and leaves the target empty.
* Integer sequences are moved past the highest copied id, so new rows do not collide.

Stop the app before running it (writes made to SQLite afterwards are not copied), and start the app
once on the current code first so its schema upgrade has normalised old values.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import bindparam, create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

BATCH = 1000


class MigrationError(Exception):
    pass


@dataclass
class Report:
    counts: dict[str, tuple[int, int]] = field(default_factory=dict)   # table -> (source, target)
    applied: bool = False
    integrity_ok: bool | None = None

    @property
    def total(self) -> int:
        return sum(s for s, _ in self.counts.values())


def source_engine(url: str) -> Engine:
    """SQLite, opened read-only (``mode=ro``) so the source file cannot change."""
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        raise MigrationError("--source must be a sqlite:/// URL.")
    if not parsed.database or parsed.database == ":memory:":
        raise MigrationError("--source needs a database file.")
    path = Path(parsed.database).expanduser().resolve()
    if not path.exists():
        raise MigrationError(f"Source database not found: {path}")
    return create_engine(f"sqlite:///file:{path}?mode=ro&uri=true", future=True)


def target_engine(url: str) -> Engine:
    parsed = make_url(url)
    if not parsed.drivername.startswith("postgresql"):
        raise MigrationError("--target must be a postgresql+psycopg:// URL.")
    return create_engine(url, future=True, pool_pre_ping=True)


def _ordered_tables(metadata):
    """Parents first. A cycle (accounts <-> bola_draws) is broken only on a NULLABLE foreign key,
    which ``_deferred_columns`` then fills in after all rows exist; a required FK is never skipped."""
    tables = {t.name: t for t in metadata.tables.values()}
    deps: dict[str, dict[str, bool]] = {n: {} for n in tables}       # table -> {parent: required?}
    for t in tables.values():
        for col in t.columns:
            for fk in col.foreign_keys:
                parent = fk.column.table.name
                if parent != t.name:
                    deps[t.name][parent] = deps[t.name].get(parent, False) or not col.nullable
    order: list[str] = []
    left = sorted(tables)
    while left:
        ready = [n for n in left if all(d in order for d in deps[n])]
        if not ready:      # a cycle: take a table whose only unmet parents are optional
            ready = [n for n in left if all(d in order or not req for d, req in deps[n].items())][:1]
        if not ready:
            raise MigrationError("Foreign keys form a cycle of required columns; cannot order tables.")
        order.append(ready[0])
        left.remove(ready[0])
    return [tables[n] for n in order]


def _deferred_columns(table, position: dict) -> list:
    """FK columns whose target is this same table or one copied later."""
    out = []
    for col in table.columns:
        for fk in col.foreign_keys:
            if position[fk.column.table.name] >= position[table.name]:
                out.append(col)
                break
    return out


def _check_empty(conn, tables) -> None:
    for t in tables:
        if conn.scalar(select(func.count()).select_from(t)):
            raise MigrationError(
                f"Target table '{t.name}' already has rows. The target must be a new, empty "
                "database (the app has not been started against it yet).")


def _fix_sequences(conn, tables) -> None:
    for t in tables:
        pk = list(t.primary_key.columns)
        if len(pk) != 1 or pk[0].type.python_type is not int:
            continue
        name = pk[0].name
        if not _has_sequence(conn, t.name, name):
            continue
        conn.execute(text(
            f"SELECT setval(pg_get_serial_sequence('\"{t.name}\"', '{name}'), "
            f"COALESCE(MAX(\"{name}\"), 1), MAX(\"{name}\") IS NOT NULL) FROM \"{t.name}\""))


def _has_sequence(conn, table: str, column: str) -> bool:
    return bool(conn.scalar(text("SELECT pg_get_serial_sequence(:t, :c)"),
                            {"t": f'"{table}"', "c": column}))


def migrate(source_url: str, target_url: str, *, apply: bool = False,
            log: Callable[[str], None] = print) -> Report:
    import app.db.models  # noqa: F401  (register every table)
    from app.db.base import Base

    src, dst = source_engine(source_url), target_engine(target_url)
    report = Report()
    src_tables = set(inspect(src).get_table_names())
    tables = [t for t in _ordered_tables(Base.metadata) if t.name in src_tables]
    position = {t.name: i for i, t in enumerate(_ordered_tables(Base.metadata))}

    with src.connect() as sconn:
        for t in tables:
            report.counts[t.name] = (int(sconn.scalar(select(func.count()).select_from(t)) or 0), 0)
    log(f"Source: {len(tables)} tables, {report.total} rows.")
    if not apply:
        for name, (n, _) in report.counts.items():
            if n:
                log(f"  {name:<24}{n:>10}")
        log("\nDry run. Nothing was written. Re-run with --apply to copy.")
        return report

    from app.db.hardening import install_audit_guards
    from app.db.migrate import upgrade
    upgrade(dst)                                       # the schema a fresh install would get
    install_audit_guards(dst)

    with dst.connect() as conn, src.connect() as sconn:
        txn = conn.begin()
        try:
            _check_empty(conn, [t for t in _ordered_tables(Base.metadata)])
            cols_in_source = {t.name: {c["name"] for c in inspect(src).get_columns(t.name)} for t in tables}
            pending: list[tuple] = []          # deferred foreign-key updates, run after every table
            for t in tables:
                common = [c for c in t.columns if c.name in cols_in_source[t.name]]
                deferred = [c for c in _deferred_columns(t, position) if c in common]
                for c in deferred:
                    if not c.nullable:
                        raise MigrationError(f"Cannot defer NOT NULL column {t.name}.{c.name}.")
                pk = list(t.primary_key.columns)
                later: list[dict] = []
                copied = 0
                order_by = [c for c in pk]
                result = sconn.execution_options(stream_results=True).execute(
                    select(*common).order_by(*order_by))
                while True:
                    chunk = result.fetchmany(BATCH)
                    if not chunk:
                        break
                    rows = []
                    for r in chunk:
                        row = dict(zip([c.name for c in common], r))
                        if deferred:
                            vals = {c.name: row[c.name] for c in deferred if row[c.name] is not None}
                            if vals:
                                later.append({**{f"pk_{p.name}": row[p.name] for p in pk},
                                              **{f"v_{k}": v for k, v in vals.items()}, "_cols": tuple(vals)})
                            for c in deferred:
                                row[c.name] = None
                        rows.append(row)
                    conn.execute(t.insert(), rows)
                    copied += len(rows)
                for c in {k for item in later for k in item["_cols"]}:      # filled in after every table
                    items = [i for i in later if c in i["_cols"]]
                    stmt = (t.update().where(*[p == bindparam(f"pk_{p.name}") for p in pk])
                            .values({c: bindparam(f"v_{c}")}))
                    pending.append((stmt, [{**{k: v for k, v in i.items() if k.startswith("pk_")},
                                            f"v_{c}": i[f"v_{c}"]} for i in items]))
                report.counts[t.name] = (report.counts[t.name][0], copied)
                if copied:
                    log(f"  copied {t.name:<24}{copied:>10}")

            for stmt, params in pending:
                conn.execute(stmt, params)
            _fix_sequences(conn, tables)

            bad = {n: c for n, c in report.counts.items() if c[0] != c[1]}
            if bad:
                raise MigrationError(f"Row counts differ after copy: {bad}")
            from app.db.models import LedgerEntry
            if "ledger_entries" in report.counts:
                s_sum = sconn.scalar(select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0))) or 0
                t_sum = conn.scalar(select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0))) or 0
                if int(s_sum) != int(t_sum):
                    raise MigrationError(f"Ledger totals differ (source {s_sum}, target {t_sum}).")
                from app.services import ledger
                report.integrity_ok = bool(ledger.verify_integrity(Session(bind=conn))["ok"])
                if not report.integrity_ok:
                    raise MigrationError("The copied ledger fails its own integrity check.")
            txn.commit()
            report.applied = True
        except Exception:
            txn.rollback()
            raise
    log(f"\nDone: {report.total} rows copied and verified (counts, ledger totals, integrity).")
    log("Next: set DATABASE_URL to the PostgreSQL URL in .env and restart the app.")
    return report


def redact(url: str) -> str:
    return make_url(url).render_as_string(hide_password=True)
