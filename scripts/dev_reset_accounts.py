"""Dev reset: ERASE every account except the Admin and the four test accounts, then make any of those four that are missing.

    python scripts/dev_reset_accounts.py                      # dry run: counts what would be erased, writes nothing
    python scripts/dev_reset_accounts.py --apply              # erases (backs the database up first)
    python scripts/dev_reset_accounts.py --apply --teller-gcash 09XXXXXXXXX    # only needed if Iris does not exist yet

Keeps:   admin, and (if they exist and are active) Xenna (Master), Ciel (Declarator), Iris (Teller), Stella (player under Xenna).
Creates: whichever of those four is missing, with the SAME password as the Admin (its password hash is copied).
Erases:  every other user and player for good, plus everything that hangs off them: wallets, sessions, tokens, tickets,
         cash requests, commissions, and every ledger transaction that touched one of their wallets. Cached balances of the
         surviving accounts are then recomputed from what is left, so the ledger stays balanced. The audit trail keeps its
         rows (the database guarantees it is append-only); they only lose the link to the deleted user, the username text stays.
Fights, draws, events, locations and settings are NOT touched.

Dev only: refuses to run unless ENV=dev. A dry run executes the whole erase inside a transaction and rolls it back.
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import secrets
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))   # run from anywhere

from sqlalchemy import delete, select, text, update  # noqa: E402

import app.db.models  # noqa: E402,F401  (registers every table)
from app.core.config import get_settings  # noqa: E402
from app.core.errors import StelError  # noqa: E402
from app.core.settings_registry import SettingsStore  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.models import Account, GcashAccount, LedgerEntry, LedgerTxn, Player, Role, Status, User  # noqa: E402
from app.db.session import engine, session_scope  # noqa: E402
from app.services import agents, ledger, players  # noqa: E402

KEEP = ("Xenna", "Ciel", "Iris", "Stella")
T = Base.metadata.tables


def backup() -> str | None:
    url = engine.url
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    src = pathlib.Path(url.database)
    dst = src.with_name(f"{src.name}.before-reset-{dt.datetime.now():%Y%m%d-%H%M%S}")
    a, b = sqlite3.connect(src), sqlite3.connect(dst)
    with b:
        a.backup(b)                      # consistent copy of the committed state (WAL), taken before we commit
    a.close(); b.close()
    return str(dst)


class Eraser:
    """Deletes rows and, first, every row that points at them (RESTRICT / CASCADE), nulling SET NULL links."""

    def __init__(self, db):
        self.db, self.seen = db, defaultdict(set)

    @staticmethod
    def children(table):
        for t in T.values():
            for fk in t.foreign_keys:
                if fk.column.table is table:
                    yield t, fk.parent, (fk.ondelete or "NO ACTION").upper()

    def erase(self, table, pks) -> None:
        pks = set(pks) - self.seen[table.name]
        if not pks:
            return
        if len(table.primary_key.columns) != 1:
            raise RuntimeError(f"{table.name} has a composite key; this script does not know how to erase it")
        pk = next(iter(table.primary_key.columns))
        self.seen[table.name] |= pks
        for child, col, action in self.children(table):
            cpk = next(iter(child.primary_key.columns))
            rows = set(self.db.scalars(select(cpk).where(col.in_(pks))).all())
            if not rows:
                continue
            if action == "SET NULL":
                self.db.execute(update(child).where(col.in_(pks)).values({col.name: None}))
            else:
                if col.nullable:                           # rows already being erased may point at each other
                    self.db.execute(update(child).where(col.in_(pks), cpk.in_(self.seen[child.name])).values({col.name: None}))
                self.erase(child, rows)
        self.db.execute(delete(table).where(pk.in_(pks)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    ap.add_argument("--teller-gcash", help="Iris's GCash number (11 digits starting 09); only needed when Iris has to be created")
    ap.add_argument("--player-gcash", default="09170000001", help="Stella's GCash number if she has to be created (placeholder)")
    ap.add_argument("--player-birth", default="1995-01-01", help="Stella's birth date if she has to be created (an adult)")
    args = ap.parse_args()

    if get_settings().ENV != "dev":
        sys.exit("Refusing to run: this erases accounts and is for ENV=dev only.")

    with session_scope() as db:
        admin = db.scalar(select(User).where(User.role == Role.ADMIN))
        if admin is None:
            sys.exit("No Admin account found.")

        # ---- who stays
        have = {u.username.lower(): u for u in db.scalars(select(User).where(User.status == Status.ACTIVE)).all()}
        have_p = {p.username.lower(): p for p in db.scalars(select(Player).where(Player.status == Status.ACTIVE)).all() if p.username}
        keep_u = {admin.id} | {have[n.lower()].id for n in KEEP if n.lower() in have and have[n.lower()].role != Role.ADMIN}
        keep_p = {have_p[n.lower()].id for n in KEEP if n.lower() in have_p}
        present = {n for n in KEEP if n.lower() in have or n.lower() in have_p}
        missing = [n for n in KEEP if n not in present]
        if "Iris" in missing and not args.teller_gcash:
            sys.exit("Iris does not exist yet: add --teller-gcash 09XXXXXXXXX (a number you control).")

        vic_u = set(db.scalars(select(User.id)).all()) - keep_u
        vic_p = set(db.scalars(select(Player.id)).all()) - keep_p
        keep_acct = set(db.scalars(select(Account.id).where(
            Account.owner_user_id.in_(keep_u) | Account.owner_player_id.in_(keep_p))).all())
        vic_acct = set(db.scalars(select(Account.id).where(
            Account.owner_user_id.in_(vic_u) | Account.owner_player_id.in_(vic_p))).all()) if (vic_u or vic_p) else set()

        # every ledger transaction that touched a doomed wallet goes whole (so each remaining txn still sums to zero)
        txns = set(db.scalars(select(LedgerEntry.txn_id).where(LedgerEntry.account_id.in_(vic_acct))).all()) if vic_acct else set()
        while txns:                                        # plus any reversal partner
            more = set(db.scalars(select(LedgerTxn.id).where(
                LedgerTxn.reverses_txn_id.in_(txns) | LedgerTxn.reversed_by_txn_id.in_(txns))).all())
            if more <= txns:
                break
            txns |= more

        # ---- erase (SQLite: foreign keys are checked at commit, since rows being erased may point at each other)
        if engine.dialect.name == "sqlite":
            db.execute(text("PRAGMA defer_foreign_keys=ON"))
        er = Eraser(db)
        er.erase(T["ledger_txns"], txns)
        if vic_u:
            er.erase(T["gcash_accounts"], db.scalars(select(GcashAccount.id).where(GcashAccount.owner_id.in_(vic_u))).all())
        er.erase(T["accounts"], vic_acct)
        er.erase(T["players"], vic_p)
        er.erase(T["users"], vic_u)
        db.flush()

        for name, ids in (("users", keep_u), ("players", keep_p), ("accounts", keep_acct)):   # nothing we keep may be swept up
            hit = er.seen[name] & ids
            if hit:
                raise RuntimeError(f"Aborted: the erase would have taken {len(hit)} {name} that must stay (nothing was changed).")
        fixed = 0                                          # cached balances of what is left, recomputed from the ledger
        for row in ledger.verify_integrity(db)["balance_mismatches"]:
            acct = db.scalar(select(Account).where(Account.code == row["account"]))
            acct.balance_cents, acct.version, fixed = row["ledger"], acct.version + 1, fixed + 1
        db.flush()
        report = ledger.verify_integrity(db)
        orphans = db.execute(text("PRAGMA foreign_key_check")).all() if engine.dialect.name == "sqlite" else []

        print(f"Admin '{admin.username}' stays." + (f" Also kept: {', '.join(n for n in KEEP if n in present)}." if present else ""))
        print(f"Erasing {len(er.seen['users'])} user(s), {len(er.seen['players'])} player(s), {len(er.seen['accounts'])} wallet(s), "
              f"{len(er.seen['ledger_txns'])} ledger transaction(s).")
        extra = {k: len(v) for k, v in er.seen.items() if v and k not in ("users", "players", "accounts", "ledger_txns", "ledger_entries")}
        if extra:
            print("  also: " + ", ".join(f"{n} {k}" for k, n in sorted(extra.items())))
        print(f"Ledger afterwards: {'balanced' if report['ok'] else 'NOT BALANCED ' + str(report)}; "
              f"{fixed} cached balance(s) recomputed; dangling references: {len(orphans)}.")
        if missing:
            print("Will create: " + ", ".join(missing))
        if not report["ok"] or orphans:
            db.rollback(); sys.exit("Aborted, nothing was changed: the result would not be consistent.")
        if not args.apply:
            db.rollback(); print("\nDry run only (nothing changed). Add --apply to do it."); return
        print("Backup:", backup() or "(not a SQLite file, skipped; take your own backup)")
        db.commit()

    # ---- create whichever of the four is missing
    if missing and args.apply:
        with session_scope() as db:
            admin = db.scalar(select(User).where(User.role == Role.ADMIN))
            store = SettingsStore(db)
            temp = secrets.token_urlsafe(18) + "aA1!"        # only to satisfy the creation form; replaced below
            make = lambda **kw: agents.create_agent(db, admin, password=temp, settings=store, **kw)  # noqa: E731
            made = {}
            if "Xenna" in missing:
                made["Xenna"] = make(username="Xenna", full_name="Xenna", role=Role.MASTER)
            if "Ciel" in missing:
                made["Ciel"] = make(username="Ciel", full_name="Ciel", role=Role.DECLARATOR)
            if "Iris" in missing:
                made["Iris"] = make(username="Iris", full_name="Iris", role=Role.TELLER, gcash_number=args.teller_gcash)
            for u in made.values():
                u.password_hash, u.must_change_password = admin.password_hash, False
            if "Stella" in missing:
                xenna = made.get("Xenna") or db.scalar(select(User).where(User.username == "Xenna"))
                stella = players.register_online_player(
                    db, admin, username="Stella", password=temp, full_name="Stella",
                    birth_date=dt.date.fromisoformat(args.player_birth), gcash_number=args.player_gcash,
                    settings=store, agent_id=xenna.id)
                stella.password_hash, stella.kyc_verified = admin.password_hash, True
            print("Created: " + ", ".join(missing))
    print("\nDone." if args.apply else "")


if __name__ == "__main__":
    try:
        main()
    except StelError as exc:
        sys.exit(f"Stopped: {getattr(exc, 'message', exc)}")
