"""Engine / session management.

Fixes the original ``incDB.php`` flaw where every helper opened a *new* mysqli
connection and the ``mysqli_close()`` after ``return`` was unreachable, so
connections leaked. Here: one pooled engine per process, one session per unit of
work, always closed.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_settings = get_settings()


def pg_connect_args(settings) -> dict:
    """PgBouncer in transaction mode hands a different server connection to every transaction, so psycopg's
    server-side prepared statements (which live on one connection) must be switched off."""
    return {"prepare_threshold": None} if settings.DB_PGBOUNCER else {}


def _build_engine() -> Engine:
    url = _settings.DATABASE_URL
    if url.startswith("sqlite"):
        engine = create_engine(
            url,
            echo=_settings.DB_ECHO,
            connect_args={"check_same_thread": False, "timeout": 30},
            future=True,
        )

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _rec):  # pragma: no cover - fallback only
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        return engine

    return create_engine(
        url,
        echo=_settings.DB_ECHO,
        pool_size=_settings.DB_POOL_SIZE,
        max_overflow=_settings.DB_MAX_OVERFLOW,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args=pg_connect_args(_settings),
        future=True,
    )


engine: Engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on any exception."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency (request-scoped session)."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
