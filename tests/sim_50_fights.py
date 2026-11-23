"""Settlement simulation: N players betting on 50 games with the project's REAL services (place_bet, ledger,
settlement, commissions), then an independent re-computation of every payout and a full money reconciliation.

Run (not collected by the normal suite, name does not start with test_):
    SIM_PLAYERS=300 SIM_FIGHTS=50 python -m pytest tests/sim_50_fights.py -s -p no:cacheprovider

Writes /tmp/sim_result.json with every number the report is built from.
"""
import collections
import datetime as dt
import json
import os
import random
import statistics
import time

from sqlalchemy import func, select

from app.core.settings_registry import SettingsStore
from app.db.models import (Commission, CommissionStatus, EventStatus, Fight, Player, Result, Role, RunStatus,
                           SettlementRun, Side, Ticket, TicketStatus, User)
from app.db.session import session_scope
from app.services import accounts, agents, events, ledger, settlement
from app.services import betting
from app.services import players as player_service
from tests.conftest import PASSWORD, load_credit

N_PLAYERS = int(os.environ.get("SIM_PLAYERS", "300"))
N_FIGHTS = int(os.environ.get("SIM_FIGHTS", "50"))
SEED = int(os.environ.get("SIM_SEED", "2026"))
SIGMA = float(os.environ.get("SIM_SIGMA", "0.07"))            # how lopsided the crowd is (std dev of the Meron share)
CLIP = float(os.environ.get("SIM_CLIP", "0.62"))              # heaviest share one side can get from the crowd model
RULES = os.environ.get("SIM_RULES", "new")                    # "new" = current defaults, "old" = 1.50x refund, no bet/Draw limits

# Scripted edge-case games (only used when the event is long enough)
F_ALL_MERON = 7          # everybody bets Meron, Wala wins  -> nobody on the winning side
F_LOPSIDED = 13          # 95% on Meron, Meron wins          -> minimum-odds rule must cancel + refund
F_DRAW = 21              # a Draw result with Draw bets
F_CANCEL = 34            # result "cancelled"
F_FIX_NOW = 42           # declared, corrected right away (Meron -> Wala)
F_FIX_LATE = 12          # declared, corrected after game 40 (winners may have spent their money)
LATE_FIX_AFTER = 40


def money(c):
    return f"{c / 100:,.2f}"


def expected_for(tickets, result, cfg):
    """My own re-implementation of the payout rules (deliberately not importing the app's maths).
    tickets: [{id, side, stake}], result: meron|wala|draw|canceled -> ({id: (payout, outcome)}, kind)."""
    by = {"meron": 0, "wala": 0, "draw": 0}
    for t in tickets:
        by[t["side"]] += t["stake"]
    main = by["meron"] + by["wala"]
    refund_all = {t["id"]: (t["stake"], "refund") for t in tickets}
    if result == "canceled":
        return refund_all, "canceled"
    out = {}
    if result == "draw":
        for t in tickets:
            out[t["id"]] = ((t["stake"] * cfg["draw_odds"]) // 10000, "win") if t["side"] == "draw" \
                else (t["stake"], "refund")
        return out, "draw"
    winning = by[result]
    combined = 10000 - (10000 - cfg["platform"]) * (10000 - cfg["house"]) // 10000
    net = main - (main * combined) // 10000
    relieved = False
    if main and cfg["min_odds"] and winning and (net * 10000) // winning < cfg["min_odds"]:
        need = -((-winning * cfg["min_odds"]) // 10000)                  # smallest net that pays the floor
        reachable = {"refund": 0, "relief_tong": main - (main * cfg["platform"]) // 10000, "relief_all": main}[cfg["mode"]]
        if need > reachable:
            return refund_all, "auto_cancel"
        net, relieved = need, True
    if winning == 0:
        return {t["id"]: (0, "lose") for t in tickets}, "no_winner"
    paid = min(net, winning * cfg["ceiling"] // 10000) if cfg["ceiling"] else net
    for t in tickets:
        out[t["id"]] = ((t["stake"] * paid) // winning, "win") if t["side"] == result else (0, "lose")
    return out, "ceiling" if paid < net else ("relief" if relieved else "normal")


def test_simulation():
    rng = random.Random(SEED)
    R = {"params": {"players": N_PLAYERS, "fights": N_FIGHTS, "seed": SEED}, "unexpected_errors": [],
         "rejections": collections.Counter(), "checks": [], "guards": [], "fights": []}
    t_start = time.time()

    def check(name, ok, detail=""):
        R["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        return ok

    # ------------------------------------------------------------------ setup
    with session_scope() as db:
        store = SettingsStore(db)
        admin = db.scalar(select(User).where(User.role == Role.ADMIN))
        if RULES == "old":                                  # what the code did before the fix
            for k, v in (("payout.min_odds_bps", 15000), ("payout.min_odds_mode", "refund"), ("payout.max_stake_cents", 5000000),
                         ("payout.max_bet_pool_share_bps", 0), ("payout.draw_max_stake_cents", 0),
                         ("payout.draw_max_exposure_cents", 0), ("payout.big_bet_alert_cents", 0)):
                store.set(k, str(v), admin.id)
        cfg = {"mode": str(store.get("payout.min_odds_mode")), "platform": int(store.get("payout.platform_cut_bps")), "house": int(store.get("payout.house_cut_bps")),
               "min_odds": int(store.get("payout.min_odds_bps")), "ceiling": int(store.get("payout.max_llamado_bps")),
               "draw_odds": int(store.get("payout.draw_odds_bps")),
               "min_stake": int(store.get("payout.min_stake_cents")), "max_stake": int(store.get("payout.max_stake_cents"))}
        if os.environ.get("SIM_BASIS"):
            store.set("commission.basis", os.environ["SIM_BASIS"], admin.id)
        R["commission_basis"] = str(store.get("commission.basis"))
        assert cfg["ceiling"] == int(store.get("payout.max_dehado_bps")), "recompute assumes equal ceilings"
        assert str(store.get("payout.no_winner_policy")) == "house" and str(store.get("payout.rounding_remainder")) == "house"
        cfg.update(pool_share=int(store.get("payout.max_bet_pool_share_bps")), draw_stake=int(store.get("payout.draw_max_stake_cents")),
                   draw_total=int(store.get("payout.draw_max_exposure_cents")))
        R["settings"] = dict(cfg, rules=RULES, sigma=SIGMA)

        master = agents.create_agent(db, admin, username="simmaster", password=PASSWORD, role=Role.MASTER,
                                     full_name="Sim Master", settings=store)
        agent_ids, sub_ids = [], []
        for i in range(3):
            a = agents.create_agent(db, admin, username=f"simagent{i}", password=PASSWORD, role=Role.AGENT,
                                    full_name=f"Sim Agent {i}", settings=store, parent_id=master.id)
            agent_ids.append(a.id)
            for j in range(2):
                s = agents.create_agent(db, admin, username=f"simsub{i}{j}", password=PASSWORD, role=Role.SUB_AGENT,
                                        full_name=f"Sim Sub {i}{j}", settings=store, parent_id=a.id)
                sub_ids.append(s.id)
        player_ids, funded = [], {}
        activity = {}
        for k in range(N_PLAYERS):
            p = player_service.register_online_player(
                db, admin, username=f"simp{k:03d}", password=PASSWORD, full_name=f"Sim Player {k}",
                birth_date=dt.date(1990, 1, 1), gcash_number=f"0917{k:07d}", settings=store,
                agent_id=sub_ids[k % len(sub_ids)])
            p.kyc_verified = True
            db.flush()
            amount = rng.choices([5_000, 10_000, 20_000, 50_000], weights=[35, 35, 22, 8])[0] * 100
            load_credit(db, admin, store, p, amount)
            player_ids.append(p.id)
            funded[p.id] = amount
            activity[p.id] = min(0.95, max(0.15, rng.betavariate(3, 2.4)))
        ev = events.create_event(db, admin, title="Simulation event", event_date=dt.date.today(), settings=store,
                                 fight_count=N_FIGHTS)
        events.set_event_status(db, admin, ev.id, EventStatus.OPEN)
        fight_ids = {f.number: f.id for f in db.scalars(select(Fight).where(Fight.event_id == ev.id))}
        event_id, master_id, admin_id = ev.id, master.id, admin.id
    R["setup"] = {"deposits_cents": sum(funded.values()), "event_id": event_id, "fights": len(fight_ids)}
    assert len(fight_ids) == N_FIGHTS

    # ------------------------------------------------------------------ helpers
    final_result, interim = {}, {}

    def place(fight_no, pid, side, stake, key=None):
        t0 = time.perf_counter()
        try:
            with session_scope() as db:
                store = SettingsStore(db)
                admin = db.get(User, admin_id)
                t = betting.place_bet(db, admin, fight_id=fight_ids[fight_no], side=Side(side), stake_cents=stake,
                                      settings=store, player_id=pid, idempotency_key=key or f"sim-{fight_no}-{pid}")
                tid = t.id
            R.setdefault("bet_ms", []).append((time.perf_counter() - t0) * 1000)
            return tid, None
        except Exception as exc:  # noqa: BLE001
            return None, exc

    def classify(exc):
        msg = str(exc)
        if "insufficient" in msg.lower() or "balance" in msg.lower() or "not enough" in msg.lower():
            return "insufficient credit"
        if "too big for this fight" in msg:
            return "bet too big for the pool (limit)"
        if "Draw is full" in msg or "Draw is almost full" in msg or "A Draw bet can be at most" in msg:
            return "Draw limit"
        return f"{type(exc).__name__}: {msg[:70]}"

    def tickets_of(fight_no):
        with session_scope() as db:
            rows = db.execute(select(Ticket.id, Ticket.side, Ticket.stake_cents, Ticket.payout_cents, Ticket.status,
                                     Ticket.player_id).where(Ticket.fight_id == fight_ids[fight_no])).all()
        return [{"id": r.id, "side": r.side.value if hasattr(r.side, "value") else str(r.side), "stake": int(r.stake_cents),
                 "payout": int(r.payout_cents or 0), "status": str(r.status.value if hasattr(r.status, "value") else r.status),
                 "player": r.player_id} for r in rows]

    def settle_and_verify(fight_no, result):
        t0 = time.perf_counter()
        with session_scope() as db:
            store = SettingsStore(db)
            run = settlement.declare(db, db.get(User, admin_id), fight_id=fight_ids[fight_no], result=result, settings=store)
            totals = dict(run.totals or {})
        ms = (time.perf_counter() - t0) * 1000
        verify_fight(fight_no, result.value, totals, ms)
        return totals

    def verify_fight(fight_no, result_str, totals, ms=None):
        tks = tickets_of(fight_no)
        exp, kind = expected_for(tks, result_str, cfg)
        bad = [t["id"] for t in tks if exp[t["id"]][0] != t["payout"]]
        stuck = [t["id"] for t in tks if t["status"] == "active"]
        stake = sum(t["stake"] for t in tks)
        paid = sum(t["payout"] for t in tks)
        with session_scope() as db:
            pool_bal = accounts.pool_for_fight(db, db.get(Fight, fight_ids[fight_no])).balance_cents
        ok_conserve = (stake - paid) == int(totals.get("house_take", -1)) if tks else True
        check(f"game {fight_no}: every ticket payout equals independent recomputation ({kind})", not bad, f"mismatch ids {bad[:5]}")
        check(f"game {fight_no}: no ticket left active", not stuck, f"{stuck[:5]}")
        check(f"game {fight_no}: stakes - payouts == recorded house take", ok_conserve,
              f"stake {stake} paid {paid} house_take {totals.get('house_take')}")
        check(f"game {fight_no}: pool account back to zero", pool_bal == 0, f"pool {pool_bal}")
        by_side = collections.Counter()
        for t in tks:
            by_side[t["side"]] += t["stake"]
        row = {"n": fight_no, "result": result_str, "kind": kind, "tickets": len(tks), "gross": stake, "paid": paid,
               "house_take": stake - paid, "meron": by_side["meron"], "wala": by_side["wala"], "draw": by_side["draw"],
               "platform": int(totals.get("platform_cut", 0)), "house_cut": int(totals.get("house_cut", 0)),
               "remainder": int(totals.get("remainder", 0)), "capped_surplus": int(totals.get("capped_surplus", 0)),
               "settle_ms": ms}
        win_stake = sum(t["stake"] for t in tks if t["status"] == "settled" and t["payout"] > 0 and t["side"] == result_str)
        row["multiple"] = round(paid / win_stake, 4) if win_stake and kind in ("normal", "ceiling") else None
        interim[fight_no] = row
        return row

    def integrity(label):
        with session_scope() as db:
            rep = ledger.verify_integrity(db)
        check(f"ledger integrity {label}", rep["ok"], json.dumps({k: rep[k] for k in ("unbalanced_txns", "balance_mismatches")}))

    # ------------------------------------------------------------------ the games
    result_pick = {}
    llamado_of = {}
    for n in range(1, N_FIGHTS + 1):
        q_meron = min(CLIP, max(1 - CLIP, rng.gauss(0.5, SIGMA)))          # crowd split for this game
        llamado_of[n] = "meron" if q_meron >= 0.5 else "wala"
        if n == F_ALL_MERON:
            q_meron = 1.0
        elif n == F_LOPSIDED:
            q_meron = 0.95
        fid = fight_ids[n]
        with session_scope() as db:
            events.set_llamado(db, db.get(User, admin_id), fid, llamado_of[n])

        n_bets = 0
        order = player_ids[:]
        rng.shuffle(order)
        for pid in order:
            if rng.random() > activity[pid]:
                continue
            roll = rng.random()
            side = "draw" if (roll < 0.01 and n != F_ALL_MERON) else ("meron" if rng.random() < q_meron else "wala")
            if rng.random() < 0.012:                                    # rare big bet
                stake = rng.choices([1000, 1500, 2000, 3000, 5000, 10000], weights=[40, 20, 20, 10, 8, 2])[0] * 100
                R.setdefault("big_bets", []).append({"fight": n, "player": pid, "stake": stake, "side": side})
            else:
                stake = rng.choices([100, 150, 200, 250, 300, 400, 500], weights=[28, 8, 24, 8, 16, 8, 8])[0] * 100
            tid, exc = place(n, pid, side, stake)
            if exc is None:
                n_bets += 1
            else:
                kind = classify(exc)
                R["rejections"][kind] += 1
                if kind not in ("insufficient credit", "bet too big for the pool (limit)", "Draw limit"):
                    R["unexpected_errors"].append({"op": f"bet g{n} p{pid}", "err": kind})
        if n == F_DRAW:                                                 # make sure Draw bettors exist
            for pid in order[:12]:
                place(n, pid, "draw", 20000, key=f"sim-draw-{n}-{pid}")

        # ---- guard checks on a few games
        if n in (3, 4):
            pid = order[0]
            t1, _ = place(n, pid, "meron", 20000, key=f"idem-{n}")
            t2, _ = place(n, pid, "meron", 20000, key=f"idem-{n}")
            R["guards"].append({"name": "same idempotency key twice gives one ticket", "ok": t1 is not None and t1 == t2})
            _, e = place(n, pid, "meron", 1000, key=f"low-{n}")
            R["guards"].append({"name": "bet under the minimum is refused", "ok": e is not None, "detail": classify(e) if e else ""})
            _, e = place(n, pid, "meron", cfg["max_stake"] + 100, key=f"high-{n}")
            R["guards"].append({"name": "bet over the maximum is refused", "ok": e is not None, "detail": classify(e) if e else ""})
            with session_scope() as db:
                bals = {i: int(accounts.wallet_for_player(db, db.get(Player, i)).balance_cents) for i in player_ids}
                poor = min(bals, key=bals.get)
                bal = bals[poor]
            _, e = place(n, poor, "meron", bal + 100, key=f"over-{n}")
            R["guards"].append({"name": "bet bigger than the wallet is refused", "ok": e is not None, "detail": classify(e) if e else ""})
        if n == 5:
            with session_scope() as db:
                events.lock_fight(db, db.get(User, admin_id), fight_ids[n], True)
            _, e = place(n, order[1], "meron", 20000, key="late-5")
            R["guards"].append({"name": "bet on a locked game is refused", "ok": e is not None, "detail": classify(e) if e else ""})

        # ---- result
        if n == F_ALL_MERON:
            res = Result.WALA
        elif n == F_LOPSIDED:
            res = Result.MERON
        elif n == F_DRAW:
            res = Result.DRAW
        elif n == F_CANCEL:
            res = Result.CANCELED
        elif n == F_FIX_NOW:
            res = Result.MERON
        elif n == F_FIX_LATE:
            res = Result.MERON
        else:
            res = Result(llamado_of[n]) if rng.random() < 0.58 else Result("wala" if llamado_of[n] == "meron" else "meron")
        result_pick[n] = res.value
        totals = settle_and_verify(n, res)
        final_result[n] = res.value

        if n == 5:
            try:
                with session_scope() as db:
                    settlement.declare(db, db.get(User, admin_id), fight_id=fight_ids[n], result=Result.WALA,
                                       settings=SettingsStore(db))
                R["guards"].append({"name": "declaring an already settled game is refused", "ok": False})
            except Exception as exc:  # noqa: BLE001
                R["guards"].append({"name": "declaring an already settled game is refused", "ok": True, "detail": classify(exc)})
            _, e = place(n, order[2], "meron", 20000, key="after-5")
            R["guards"].append({"name": "bet on a settled game is refused", "ok": e is not None, "detail": classify(e) if e else ""})

        def correct(fight_no, new):
            with session_scope() as db:
                store = SettingsStore(db)
                run = settlement.request_correction(db, db.get(User, admin_id), fight_id=fight_ids[fight_no],
                                                    result=Result(new), reason="simulation: result corrected",
                                                    settings=store)
                run_id = run.id
            try:                                                        # the requester cannot approve their own correction
                with session_scope() as db:
                    settlement.approve(db, db.get(User, admin_id), run_id=run_id, settings=SettingsStore(db))
                R["guards"].append({"name": "requester cannot approve own correction (maker-checker)", "ok": False})
            except Exception:  # noqa: BLE001
                R["guards"].append({"name": "requester cannot approve own correction (maker-checker)", "ok": True})
            try:
                with session_scope() as db:
                    run = settlement.approve(db, db.get(User, master_id), run_id=run_id, settings=SettingsStore(db))
                    tot = dict(run.totals or {})
            except Exception as exc:  # noqa: BLE001
                # FINDING: the claw-back of the old payouts needs more money than a winner still has (they spent it).
                # ledger.post(force=True) is meant to record that as a debt, but the database refuses a negative wallet.
                R.setdefault("late_correction_failures", []).append(
                    {"fight": fight_no, "error": f"{type(exc).__name__}: {str(exc)[:140]}"})
                return
            final_result[fight_no] = new
            verify_fight(fight_no, new, tot)

        if n == F_FIX_NOW:
            correct(n, "wala")
        if n == LATE_FIX_AFTER and F_FIX_LATE <= N_FIGHTS:
            correct(F_FIX_LATE, "wala")
        if n % 10 == 0:
            integrity(f"after game {n}")
        print(f"game {n:2d}/{N_FIGHTS} {res.value:8s} bets={n_bets:3d} -> {interim[n]['kind']}", flush=True)

    # ------------------------------------------------------------------ final reconciliation
    integrity("at the end")
    with session_scope() as db:
        wallets = {pid: int(accounts.wallet_for_player(db, db.get(Player, pid)).balance_cents) for pid in player_ids}
        house = int(accounts.house(db).balance_cents)
        comm_exp = int(accounts.commission_expense(db).balance_cents)
        gcash = int(accounts.gcash_clearing(db).balance_cents)
        runs = db.scalars(select(SettlementRun).where(SettlementRun.status == RunStatus.APPLIED)).all()
        applied_totals = {r.fight_id: dict(r.totals or {}) for r in runs}
        run_versions = collections.Counter(r.fight_id for r in db.scalars(select(SettlementRun)).all())
        comm_rows = db.execute(select(Commission.tier, func.sum(Commission.amount_cents), func.count())
                               .where(Commission.status != CommissionStatus.REVERSED).group_by(Commission.tier)).all()
        comm_reversed = db.scalar(select(func.count()).select_from(Commission).where(Commission.status == CommissionStatus.REVERSED))
        comm_acct_total = 0
        for uid in [master_id] + agent_ids + sub_ids:
            comm_acct_total += int(accounts.commission_for_user(db, db.get(User, uid)).balance_cents)
        fids = set(fight_ids.values())
        pool_total = sum(int(accounts.pool_for_fight(db, db.get(Fight, f)).balance_cents) for f in fids)
        all_tickets = db.execute(select(Ticket.id, Ticket.fight_id, Ticket.player_id, Ticket.side, Ticket.stake_cents,
                                        Ticket.payout_cents, Ticket.status)).all()

    # independent player reconciliation: wallet must equal deposit - stakes + payouts
    expected_wallet = {pid: funded[pid] for pid in player_ids}
    for t in all_tickets:
        expected_wallet[t.player_id] += int(t.payout_cents or 0) - int(t.stake_cents)
    mism = {pid: (wallets[pid], expected_wallet[pid]) for pid in player_ids if wallets[pid] != expected_wallet[pid]}
    check("every one of the players' wallets == deposit - stakes + payouts", not mism, f"{len(mism)} mismatches: {list(mism.items())[:3]}")
    negative = {pid: w for pid, w in wallets.items() if w < 0}
    R["negative_wallets"] = {"count": len(negative), "total_cents": sum(negative.values()),
                             "worst_cents": min(negative.values()) if negative else 0}
    status_counts = collections.Counter(str(t.status.value if hasattr(t.status, "value") else t.status) for t in all_tickets)
    check("no ticket left active", status_counts.get("active", 0) == 0, str(dict(status_counts)))
    # final per-ticket check against the FINAL result of each game
    bad_final = 0
    for n, fid in fight_ids.items():
        tks = [{"id": t.id, "side": t.side.value if hasattr(t.side, "value") else str(t.side), "stake": int(t.stake_cents)}
               for t in all_tickets if t.fight_id == fid]
        if not tks:
            continue
        exp, _ = expected_for(tks, final_result[n], cfg)
        bad_final += sum(1 for t in all_tickets if t.fight_id == fid and exp[t.id][0] != int(t.payout_cents or 0))
    check("final state: every ticket of every game equals the independent recomputation", bad_final == 0, f"{bad_final} bad")
    total_deposits = sum(funded.values())
    check("money conservation: deposits == player wallets + house revenue + open pools",
          total_deposits == sum(wallets.values()) + house + pool_total,
          f"deposits {total_deposits} wallets {sum(wallets.values())} house {house} pools {pool_total}")
    house_from_runs = sum(int(t.get("house_take", 0)) for t in applied_totals.values())
    check("house account == sum of house take of the final settlement runs", house == house_from_runs,
          f"house {house} runs {house_from_runs}")
    comm_total = sum(int(r[1] or 0) for r in comm_rows)
    check("commission rows == commission expense == commission accounts", comm_total == comm_exp == comm_acct_total,
          f"rows {comm_total} expense {comm_exp} accounts {comm_acct_total}")
    with session_scope() as db:                                    # the cap is per game: commission <= that game's house win
        over = []
        for r in runs:
            c = int(db.scalar(select(func.coalesce(func.sum(Commission.amount_cents), 0)).where(
                Commission.run_id == r.id, Commission.status != CommissionStatus.REVERSED)) or 0)
            if c > max(0, int((r.totals or {}).get("house_take", 0))):
                over.append((r.fight_id, c, (r.totals or {}).get("house_take")))
    check("commission of each game never exceeds that game's house win (cap)", not over, f"{over[:3]}")
    R["net_house_after_commission"] = house_from_runs - comm_total
    check("online payments clearing == total deposits (money in)", gcash == total_deposits or gcash == -total_deposits,
          f"clearing {gcash} deposits {total_deposits}")

    # breakdown identity for every normal pool game: house take = platform + house cut + ceiling surplus + remainder + lost draw stakes
    ident_bad = []
    for n, row in interim.items():
        if row["kind"] in ("normal", "ceiling", "relief"):
            fin = tickets_of(n)
            lost_draw = sum(t["stake"] for t in fin if t["side"] == "draw")
            if final_result[n] != row["result"]:
                continue
            if row["house_take"] != row["platform"] + row["house_cut"] + row["capped_surplus"] + row["remainder"] + lost_draw:
                ident_bad.append(n)
    check("house take == platform cut + house commission + ceiling surplus + rounding + lost Draw stakes (pool games)",
          not ident_bad, f"games {ident_bad}")

    # ------------------------------------------------------------------ numbers for the report
    final_rows = []
    for n in range(1, N_FIGHTS + 1):
        row = dict(interim[n])
        row["final_result"] = final_result[n]
        row["versions"] = run_versions[fight_ids[n]]
        final_rows.append(row)
    R["fights"] = final_rows
    kinds = collections.Counter()
    for n in range(1, N_FIGHTS + 1):
        t = applied_totals.get(fight_ids[n], {})
        kinds["auto_cancelled (min odds)" if t.get("auto_cancel") else ("cancelled/refunded" if final_result[n] == "canceled" else final_result[n] if final_result[n] in ("draw",) else "settled")] += 1
    R["game_kinds"] = dict(kinds)
    tot = collections.Counter()
    for t in applied_totals.values():
        for k in ("total", "platform_cut", "house_cut", "remainder", "capped_surplus", "relief", "house_take", "payout_total", "refund_total"):
            tot[k] += int(t.get(k, 0))
    R["totals"] = dict(tot)
    R["games_with_relief"] = sum(1 for f in final_rows if f["kind"] == "relief")
    R["games_refunded_by_rule"] = sum(1 for f in final_rows if f["kind"] == "auto_cancel")
    R["totals"]["commission_total"] = comm_total
    R["commission_by_tier"] = {r[0]: int(r[1] or 0) for r in comm_rows}
    R["commission_rows_reversed"] = comm_reversed
    R["house_balance"] = house
    R["total_deposits"] = total_deposits
    R["wallets_total"] = sum(wallets.values())
    R["status_counts"] = dict(status_counts)
    stake_total = sum(int(t.stake_cents) for t in all_tickets)
    R["tickets"] = {"count": len(all_tickets), "stake_total": stake_total}
    pnl = {pid: wallets[pid] - funded[pid] for pid in player_ids}
    R["players"] = {"ahead": sum(1 for v in pnl.values() if v > 0), "behind": sum(1 for v in pnl.values() if v < 0),
                    "even": sum(1 for v in pnl.values() if v == 0), "best": max(pnl.values()), "worst": min(pnl.values()),
                    "median": statistics.median(pnl.values()), "all_funds_lost": sum(1 for pid in player_ids if wallets[pid] < 2000)}
    big = R.get("big_bets", [])
    R["big_bets_summary"] = {"count": len(big), "volume_cents": sum(b["stake"] for b in big),
                             "share_of_volume": round(sum(b["stake"] for b in big) / stake_total, 4) if stake_total else 0,
                             "largest_cents": max([b["stake"] for b in big], default=0)}
    bet_ms = R.get("bet_ms", [])
    R["timing"] = {"total_s": round(time.time() - t_start, 1), "bets": len(bet_ms),
                   "bet_ms_avg": round(statistics.mean(bet_ms), 1) if bet_ms else 0,
                   "bet_ms_p95": round(sorted(bet_ms)[int(len(bet_ms) * .95)], 1) if bet_ms else 0,
                   "settle_ms_avg": round(statistics.mean(r["settle_ms"] for r in final_rows if r["settle_ms"]), 1),
                   "settle_ms_max": round(max(r["settle_ms"] for r in final_rows if r["settle_ms"]), 1)}
    R["rejections"] = dict(R["rejections"])
    R["checks_total"] = len(R["checks"])
    R["checks_failed"] = [c for c in R["checks"] if not c["ok"]]
    R["checks"] = [c for c in R["checks"] if not c["ok"]] + [{"name": f"{len(R['checks']) - len(R['checks_failed'])} checks passed", "ok": True}]
    with open(os.environ.get("SIM_OUT", "/tmp/sim_result.json"), "w") as fh:
        json.dump(R, fh, indent=1, default=str)
    print("SIM DONE", json.dumps({"checks_total": R["checks_total"], "failed": len(R["checks_failed"]),
                                  "unexpected_errors": len(R["unexpected_errors"]), "timing": R["timing"]}), flush=True)
