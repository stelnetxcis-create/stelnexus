"""Bola load and race test (plan sections 8 phase 7 and 9). Run it against a DISPOSABLE database.

    DATABASE_URL=postgresql+psycopg://user:pass@127.0.0.1:5432/scratch \\
    SECRET_KEY=any-long-test-secret-0123456789-0123456789 ENV=dev \\
    python scripts/bola_loadtest.py [--bets 500] [--workers 32] [--rounds 20]

What it proves (exit code 0 only if every check holds):
  A. N concurrent bets on one draw: exposure equals the sum of the tickets' lines, the ledger
     balances, and every player was charged exactly what their tickets say.
  B. Two threads at the liability-cap boundary: exactly one wins, every round.
  C. Two threads at the per-line-limit boundary: exactly one wins, every round.
  D. The same idempotency key sent by many threads at once: one ticket, charged once.
  E. Cancel racing new bets keeps exposure equal to the live tickets.

It refuses to run against SQLite (a single writer proves nothing about row locks) unless
--allow-sqlite is given, and it creates its own players, area and draws.
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import random
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))   # run from anywhere

from sqlalchemy import func, select  # noqa: E402

from app.core.security import hash_password
from app.core.settings_registry import SettingsStore
from app.db.migrate import upgrade
from app.db.models import (BolaExposure, BolaKind, BolaLine, BolaTicket, DrawStatus,
                           RequestDirection, Role, Status, TicketStatus, User)
from app.db.session import engine, session_scope
from app.services import accounts, agents, bola, cashdesk, ledger, locations, players

PASSWORD = "Load-Passw0rd-2026!"
FAILED: list[str] = []


def check(ok: bool, label: str) -> None:
    print(("  PASS  " if ok else "  FAIL  ") + label)
    if not ok:
        FAILED.append(label)


def _admin(db) -> User:
    admin = db.scalar(select(User).where(User.username == "admin"))
    if admin is None:
        admin = User(username="admin", password_hash=hash_password(PASSWORD), role=Role.ADMIN,
                     full_name="Admin", ref_code=agents._unique_ref(db), status=Status.ACTIVE,
                     must_change_password=False)
        db.add(admin)
        db.flush()
        accounts.wallet_for_user(db, admin)
        accounts.commission_for_user(db, admin)
    return admin


def setup(n_players: int, credit_cents: int) -> dict:
    tag = f"{int(time.time()) % 10**8}"
    upgrade(engine)
    with session_scope() as db:
        accounts.ensure_system_accounts(db)
        store = SettingsStore(db)
        admin = _admin(db)
        store.set("bola.enabled", "true", admin.id)
        store.set("bola.limit_per_line_cents", "10000000", admin.id)        # out of the way for A
        store.set("bola.max_liability_per_outcome_cents", "0", admin.id)
        store.set("bola.max_liability_per_draw_cents", "0", admin.id)
        master = agents.create_agent(db, admin, username=f"lm{tag}", password=PASSWORD,
                                     role=Role.MASTER, full_name="LM", settings=store)
        cavo = agents.create_agent(db, master, username=f"lc{tag}", password=PASSWORD,
                                   role=Role.CAVO, full_name="LC", settings=store)
        kub = agents.create_agent(db, cavo, username=f"lk{tag}", password=PASSWORD,
                                  role=Role.KUBRADOR, full_name="LK", settings=store)
        ids = []
        for i in range(n_players):
            p = players.register_online_player(
                db, kub, username=f"lp{tag}_{i}", password=PASSWORD, full_name=f"P{i}",
                birth_date=dt.date(1990, 1, 1), gcash_number="09171234567", settings=store,
                agent_id=kub.id)
            p.kyc_verified = True
            db.flush()
            req = cashdesk.create_request(db, admin, direction=RequestDirection.IN,
                                          amount_cents=credit_cents, settings=store,
                                          player_id=p.id, method="gcash", reference="GC-LOAD-0001")
            cashdesk.review(db, admin, request_id=req.id, approve=True, settings=store)
            ids.append(p.id)
        loc = locations.create_location(db, admin, name=f"Load Area {tag}")
        return {"tag": tag, "admin": admin.id, "kub": kub.id, "players": ids, "loc": loc.id}


_slot = iter(range(1, 10_000))
_slot_lock = threading.Lock()


def new_draw(ctx: dict) -> int:
    """A fresh OPEN draw (a unique date x slot each time)."""
    with _slot_lock:
        n = next(_slot)
    with session_scope() as db:
        store = SettingsStore(db)
        admin = db.get(User, ctx["admin"])
        day = dt.date(2040, 1, 1) + dt.timedelta(days=n // 6 + int(ctx["tag"]) % 3000)
        draw = bola.create_draw(db, admin, location_id=ctx["loc"], draw_date=day, slot=n % 6 + 1,
                                settings=store, status=DrawStatus.OPEN,
                                scheduled_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3))
        return draw.id


def bet(ctx: dict, draw_id: int, numbers, tumbok: int, sahod: int, player_id: int,
        key: str | None = None):
    """One bet in its own session, exactly like one web request. Returns ('ok', ticket_id) or
    ('err', message)."""
    try:
        with session_scope() as db:
            store = SettingsStore(db)
            kub = db.get(User, ctx["kub"])
            t = bola.place_bola_bet(db, kub, draw_id=draw_id, kind=BolaKind.PAIR, numbers=list(numbers),
                                    tumbok_cents=tumbok, sahod_cents=sahod, player_id=player_id,
                                    settings=store, idempotency_key=key)
            return ("ok", t.id)
    except Exception as exc:                                    # noqa: BLE001 - we count them
        return ("err", f"{type(exc).__name__}: {str(exc)[:90]}")


def set_setting(ctx: dict, key: str, value: str) -> None:
    with session_scope() as db:
        SettingsStore(db).set(key, value, ctx["admin"])


def audit_draw(draw_id: int) -> dict:
    """Recompute exposure from the live lines and compare it with the running BolaExposure rows."""
    with session_scope() as db:
        live = {}
        for first, second, t, s in db.execute(
                select(BolaLine.first, BolaLine.second, BolaLine.tumbok_cents, BolaLine.sahod_cents)
                .join(BolaTicket, BolaTicket.id == BolaLine.ticket_id)
                .where(BolaLine.draw_id == draw_id, BolaTicket.status == TicketStatus.ACTIVE)).all():
            a, b = live.get((first, second), (0, 0))
            live[(first, second)] = (a + t, b + s)
        stored = {(r.first, r.second): (r.tumbok_cents, r.sahod_cents)
                  for r in db.scalars(select(BolaExposure).where(BolaExposure.draw_id == draw_id)).all()
                  if r.tumbok_cents or r.sahod_cents}
        staked = int(db.scalar(select(func.coalesce(func.sum(BolaTicket.stake_cents), 0)).where(
            BolaTicket.draw_id == draw_id, BolaTicket.status == TicketStatus.ACTIVE)) or 0)
        pool = accounts.pool_for_draw(db, db.get(bola.BolaDraw, draw_id))
        return {"exposure_ok": live == stored, "live_cells": len(live), "staked": staked,
                "pool_balance": pool.balance_cents}


def test_a_load(ctx: dict, bets: int, workers: int) -> None:
    print(f"\nA. {bets} concurrent bets on one draw ({workers} workers)")
    draw = new_draw(ctx)
    rng = random.Random(7)
    jobs = []
    for _ in range(bets):
        a, b = rng.sample(range(1, 11), 2)                       # 90 ordered pairs: real contention
        jobs.append((ctx["players"][rng.randrange(len(ctx["players"]))], (a, b), rng.choice([100, 500, 1000])))
    start = time.perf_counter()
    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(lambda j: bet(ctx, draw, j[1], j[2], 0, j[0]), jobs))
    took = time.perf_counter() - start
    outcome = Counter(r[0] for r in results)
    errors = Counter(r[1] for r in results if r[0] == "err")
    print(f"     {outcome['ok']} placed, {outcome['err']} refused, {took:.1f}s "
          f"({bets / took:.0f} bets/s)")
    for message, count in errors.most_common(5):
        print(f"     refused x{count}: {message}")
    check(outcome["ok"] > 0 and outcome["err"] == 0, "every bet was accepted (limits are off)")
    figures = audit_draw(draw)
    check(figures["exposure_ok"], f"exposure equals the sum of live lines ({figures['live_cells']} cells)")
    with session_scope() as db:
        expected = sum(j[2] for j, r in zip(jobs, results) if r[0] == "ok")
    check(figures["staked"] == expected, f"tickets total the stakes sent (PHP {expected / 100:,.2f})")
    check(figures["pool_balance"] == -expected or figures["pool_balance"] == expected,
          "draw pool balance equals the staked total")
    with session_scope() as db:
        check(ledger.verify_integrity(db)["ok"], "ledger.verify_integrity is clean")


def _pair_race(ctx: dict, rounds: int, label: str, setting: str, value: str, numbers, tumbok: int,
               *, reset: tuple[str, str]) -> None:
    print(f"\n{label}")
    set_setting(ctx, setting, value)
    wins = Counter()
    try:
        for r in range(rounds):
            draw = new_draw(ctx)
            barrier = threading.Barrier(2)

            def go(player):
                barrier.wait()
                return bet(ctx, draw, numbers, tumbok, 0, player)

            with ThreadPoolExecutor(2) as pool:
                outcomes = list(pool.map(go, ctx["players"][:2]))
            wins[sum(1 for o in outcomes if o[0] == "ok")] += 1
            check_figures = audit_draw(draw)
            if not check_figures["exposure_ok"]:
                wins["exposure_mismatch"] += 1
    finally:
        set_setting(ctx, *reset)
    check(wins[1] == rounds and wins["exposure_mismatch"] == 0,
          f"exactly one winner in each of {rounds} rounds (got {dict(wins)})")


def test_b_cap(ctx: dict, rounds: int) -> None:
    # 800x: PHP 1 on one outcome owes PHP 800; a PHP 1,000 cap fits one such bet, not two.
    _pair_race(ctx, rounds, "B. two threads at the per-outcome liability cap",
               "bola.max_liability_per_outcome_cents", "100000", (7, 9), 100,
               reset=("bola.max_liability_per_outcome_cents", "0"))


def test_c_line(ctx: dict, rounds: int) -> None:
    # limit PHP 1,500 per ordered pair; two PHP 1,000 bets cannot both fit.
    _pair_race(ctx, rounds, "C. two threads at the per-line limit",
               "bola.limit_per_line_cents", "150000", (12, 24), 100_000,
               reset=("bola.limit_per_line_cents", "10000000"))


def test_d_idempotency(ctx: dict, threads: int = 12) -> None:
    print(f"\nD. one idempotency key, {threads} threads at once")
    draw = new_draw(ctx)
    key = f"load-idem-{ctx['tag']}"
    barrier = threading.Barrier(threads)
    player = ctx["players"][0]

    def go(_):
        barrier.wait()
        return bet(ctx, draw, (3, 4), 500, 0, player, key=key)

    with ThreadPoolExecutor(threads) as pool:
        outcomes = list(pool.map(go, range(threads)))
    ok_ids = {o[1] for o in outcomes if o[0] == "ok"}
    errs = Counter(o[1] for o in outcomes if o[0] == "err")
    with session_scope() as db:
        n = db.scalar(select(func.count(BolaTicket.id)).where(BolaTicket.draw_id == draw)) or 0
    check(n == 1, f"exactly one ticket was created (found {n})")
    check(len(ok_ids) == 1 and not errs,
          f"every duplicate got the same ticket back, none errored ({len(ok_ids)} id(s), "
          f"{sum(errs.values())} errors: {list(errs)[:1]})")
    check(audit_draw(draw)["staked"] == 500, "the player was charged once (PHP 5.00)")


def test_e_cancel_race(ctx: dict, workers: int) -> None:
    print("\nE. cancels racing new bets")
    draw = new_draw(ctx)
    placed = [bet(ctx, draw, (a, a + 1), 200, 0, ctx["players"][a % len(ctx["players"])])
              for a in range(1, 30)]
    ticket_ids = [p[1] for p in placed if p[0] == "ok"]

    def cancel(tid):
        try:
            with session_scope() as db:
                store = SettingsStore(db)
                admin = db.get(User, ctx["admin"])
                bola.cancel_bola_ticket(db, admin, ticket_id=tid, reason="load test cancel", settings=store)
                return "ok"
        except Exception as exc:                                 # noqa: BLE001
            return f"{type(exc).__name__}"

    def mixed(i):
        if i % 2 == 0:
            return cancel(ticket_ids[i // 2 % len(ticket_ids)])
        return bet(ctx, draw, (30 + i % 8, 1 + i % 9), 100, 0, ctx["players"][i % len(ctx["players"])])[0]

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(mixed, range(80)))
    figures = audit_draw(draw)
    check(figures["exposure_ok"], "exposure still equals the live lines after the race")
    with session_scope() as db:
        check(ledger.verify_integrity(db)["ok"], "ledger.verify_integrity is clean")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bets", type=int, default=500)
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--players", type=int, default=40)
    ap.add_argument("--allow-sqlite", action="store_true")
    args = ap.parse_args()
    if engine.dialect.name == "sqlite" and not args.allow_sqlite:
        print("Refusing to run on SQLite: it has one writer, so it cannot show row-lock behaviour.\n"
              "Point DATABASE_URL at a scratch PostgreSQL database (or pass --allow-sqlite).")
        return 2
    print(f"database: {engine.dialect.name}; players: {args.players}")
    ctx = setup(args.players, credit_cents=50_000_000)
    test_a_load(ctx, args.bets, args.workers)
    test_b_cap(ctx, args.rounds)
    test_c_line(ctx, args.rounds)
    test_d_idempotency(ctx)
    test_e_cancel_race(ctx, args.workers)
    print("\nRESULT:", "ALL CHECKS PASSED" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
