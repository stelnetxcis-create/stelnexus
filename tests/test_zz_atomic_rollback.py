"""A failed request must leave NOTHING behind. On SQLite this used to break after start-up: the migration helpers put a
pooled connection into manual-commit mode and never put it back, so every later request on it was autocommit and a
rollback could not undo half-written data (found when a late result correction crashed half way)."""
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db.models import LedgerTxn, User
from app.db.session import engine, session_scope
from app.services import accounts, ledger


def test_pooled_sqlite_connections_are_not_left_in_autocommit_mode():
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite only")
    from app.db.migrate import upgrade
    upgrade(engine)                                       # runs both helpers that used to flip the mode
    conns = [engine.raw_connection() for _ in range(3)]
    try:
        assert all(c.driver_connection.isolation_level is not None for c in conns)
    finally:
        for c in conns:
            c.close()


def test_a_failed_forced_reversal_rolls_back_completely(world):
    """Winnings were spent, so taking them back would push the wallet below zero and the database refuses it.
    Whatever the request wrote before that point must be undone: no half-posted transaction, balances still true."""
    wallet = accounts.wallet_for_player(world.db, world.player)
    gcash, house = accounts.gcash_clearing(world.db), accounts.house(world.db)
    credit = ledger.post(world.db, kind="test.in", entries=[(gcash, +50_000), (wallet, -50_000)],
                         actor=world.admin, memo="in")
    spend = ledger.balance_of(world.db, wallet)
    ledger.post(world.db, kind="test.out", entries=[(wallet, +spend), (house, -spend)], actor=world.admin, memo="spend")
    world.db.commit()
    before = world.db.scalar(select(func.count()).select_from(LedgerTxn))
    report_before = ledger.verify_integrity(world.db)          # other tests share this database: compare, do not demand a clean slate

    with pytest.raises(IntegrityError):
        with session_scope() as s:
            ledger.reverse(s, s.get(LedgerTxn, credit.id), actor=s.get(User, world.admin.id),
                           reason="claw-back of spent winnings", force=True)

    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(LedgerTxn)) == before
        report = ledger.verify_integrity(s)
        assert report["unbalanced_txns"] == report_before["unbalanced_txns"], report
        assert report["balance_mismatches"] == report_before["balance_mismatches"], report
