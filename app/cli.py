"""Operations CLI.

    python -m app.cli init-db
    python -m app.cli create-admin --username admin --full-name "Site Admin"   # hidden dev account
    python -m app.cli create-master --username boss --full-name "The Boss"    # top visible account
    python -m app.cli seed-demo          # dev only: sample agents, an online player, an event
    python -m app.cli seed-locations     # dev only: 5 sample Batangas venues with pictures
    python -m app.cli gen-keys           # prints SECRET_KEY and PII_KEY values
    python -m app.cli unban-ip 203.0.113.9            # lift an IP ban from the server shell (lock-out recovery)
    python -m app.cli bola-generate --days 2          # create the number-game draws from schedules
    python -m app.cli bola-tick                       # cron, every minute: open/lock draws on time
    python -m app.cli streams-check --repeat 2        # cron, every minute: health of the video sources (about every 30 s)
    python -m app.cli sqlite-to-postgres --source sqlite:///./dev.db --target postgresql+psycopg://... [--apply]
    python -m app.cli refresh-reports [--only flows,top,integrity]   # timer: precompute heavy reports
    python -m app.cli bola-convert-cuts --rates r.csv # legacy % -> Volume/SOP bps (prints; --apply writes)
    python -m app.cli bola-import-legacy --sql online_bukis.sql   # dry run; add --apply to write
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import os
import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import StelError
from app.core.security import generate_pii_key, hash_password, password_problems
from app.core.settings_registry import SettingsStore
from app.db.base import Base
from app.db.models import Role, Status, User
from app.db.session import engine, session_scope
from app.services import accounts, agents, events, players


def init_db(_args) -> None:
    import app.db.models  # noqa: F401  (register tables)
    from app.db.migrate import upgrade
    upgrade(engine)  # create_all + add new columns / widen CHECKs on older databases
    from app.db.hardening import install_audit_guards
    install_audit_guards(engine)
    with session_scope() as db:
        accounts.ensure_system_accounts(db)
        from app.services import teams
        teams.ensure_teams(db)
    print("Database tables, system accounts and the NBA team catalog are ready.")


def create_admin(args) -> None:
    password = args.password or os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Password: ")
    with session_scope() as db:
        store = SettingsStore(db)
        if db.scalar(select(User.id).where(User.role == Role.ADMIN)):
            sys.exit("An Admin already exists. Only one Admin is allowed.")
        if db.scalar(select(User.id).where(User.username == args.username)):
            sys.exit(f"User '{args.username}' already exists.")
        problems = password_problems(password, args.username, min_length=int(store.get("security.password_min_length")))
        if problems:
            sys.exit("Weak password: " + " ".join(dict.fromkeys(problems)))
        user = User(username=args.username, password_hash=hash_password(password), role=Role.ADMIN,
                    full_name=args.full_name, ref_code=agents._unique_ref(db),
                    status=Status.ACTIVE, must_change_password=False)
        db.add(user)
        db.flush()
        accounts.ensure_system_accounts(db)
        accounts.wallet_for_user(db, user)
        accounts.commission_for_user(db, user)
    print(f"Admin '{args.username}' created.")


def create_master(args) -> None:
    password = args.password or os.environ.get("MASTER_PASSWORD") or getpass.getpass("Password: ")
    with session_scope() as db:
        store = SettingsStore(db)
        admin = db.scalar(select(User).where(User.role == Role.ADMIN))
        if admin is None:
            sys.exit("Create the hidden admin first (create-admin).")
        user = agents.create_agent(db, admin, username=args.username, password=password,
                                   role=Role.MASTER, full_name=args.full_name, settings=store)
        user.must_change_password = False
    print(f"Master '{args.username}' created.")


def create_pawn(args) -> None:
    password = args.password or os.environ.get("PAWN_PASSWORD") or getpass.getpass("Password: ")
    with session_scope() as db:
        admin = db.scalar(select(User).where(User.role == Role.ADMIN))
        if admin is None:
            sys.exit("Create the hidden admin first (create-admin).")
        agents.create_agent(db, admin, username=args.username, password=password, role=Role.PAWN,
                            full_name=args.full_name, settings=SettingsStore(db))
    print(f"Pawn '{args.username}' created. Choose what it may see in Admin Panel -> Pawns.")


def seed_demo(_args) -> None:
    if get_settings().ENV != "dev":
        sys.exit("seed-demo only runs with ENV=dev.")
    demo_pw = "Demo-Passw0rd-2026!"
    with session_scope() as db:
        store = SettingsStore(db)
        admin = db.scalar(select(User).where(User.role == Role.ADMIN))
        if admin is None:
            sys.exit("Create an admin first (create-admin).")
        master = agents.create_agent(db, admin, username="demo_master", password=demo_pw,
                                     role=Role.MASTER, full_name="Demo Master", settings=store)
        agent = agents.create_agent(db, master, username="demo_agent", password=demo_pw,
                                    role=Role.AGENT, full_name="Demo Agent", settings=store)
        sub_agent = agents.create_agent(db, agent, username="demo_subagent", password=demo_pw,
                                        role=Role.SUB_AGENT, full_name="Demo Sub Agent", settings=store)
        players.register_online_player(db, admin, username="demo_player", password=demo_pw,
                                       full_name="Demo Player", birth_date=dt.date(1990, 1, 1),
                                       gcash_number="09170000001", settings=store,
                                       agent_id=sub_agent.id)
        events.create_event(db, admin, title="Demo Event", event_date=dt.date.today(),
                            settings=store, fight_count=5)
    print(f"Demo data created. Demo users share the password: {demo_pw}")


def seed_locations(args) -> None:
    """Dev only: five sample Batangas venues with pictures; events without a location get one."""
    if get_settings().ENV != "dev":
        sys.exit("seed-locations only runs with ENV=dev.")
    from app.services import locations
    with session_scope() as db:
        admin = db.scalar(select(User).where(User.role == Role.ADMIN))
        if admin is None:
            sys.exit("Create an admin first (create-admin).")
        done = locations.seed_samples(db, admin, attach_events=not args.no_events)
    print(f"Created {len(done['created'])} venue(s): {', '.join(done['created']) or 'none (already there)'}")
    if done["pictured"]:
        print(f"Added a picture to: {', '.join(done['pictured'])}")
    print(f"Events given a venue: {done['events_attached']}")


def gen_keys(_args) -> None:
    from app.core.config import generate_secret
    print(f"SECRET_KEY={generate_secret()}")
    print(f"PII_KEY={generate_pii_key()}")


def encrypt_pii(_args) -> None:
    """Encrypt player address / GCash values that were stored before PII_KEY was set."""
    from app.core.security import pii
    from app.db.models import Player
    if not pii.enabled:
        sys.exit("PII_KEY is not set, nothing to encrypt with.")
    done = 0
    with session_scope() as db:
        for player in db.scalars(select(Player)).all():
            for col in ("address_enc", "gcash_enc"):
                value = getattr(player, col)
                if value and not value.startswith("enc:v1:"):
                    setattr(player, col, pii.encrypt(value))
                    done += 1
    print(f"Encrypted {done} stored value(s).")


def convert_cut(com: float, sop: float, pool: float) -> tuple[int, int]:
    """Legacy per-Cavo rates ("% of sales") -> StelNexus (Volume bps of the pool, SOP bps of the
    Agent's cut; the old Cavo is now an Agent). Assumes SOP is paid OUT of the Agent's cut (plan D3). Raises ValueError if the
    pair cannot be expressed (no commission but a SOP, SOP above the commission, pool too small)."""
    if pool <= 0:
        raise ValueError("pool must be above 0")
    if com <= 0:
        raise ValueError("commission is 0 -> unset in the old panel; needs your ruling")
    if com > pool:
        raise ValueError(f"commission {com} is above the pool {pool}; raise --pool")
    if sop > com:
        raise ValueError(f"SOP {sop} is larger than the commission {com}; cannot be paid out of it")
    return round(com / pool * 10000), round(sop / com * 10000)


def bola_generate(args) -> None:
    from app.services import bola
    with session_scope() as db:
        store = SettingsStore(db)
        if not store.get("bola.enabled"):
            print("Number game is switched off (bola.enabled=false): nothing to do.")
            return
        start = dt.date.fromisoformat(args.date) if args.date else None
        out = bola.generate_draws(db, None, settings=store, date=start, days=args.days)
        print(f"Created {len(out['created'])} draw(s), skipped {out['skipped']} that already existed.")


def bola_preflight(_args) -> None:
    """Exit 1 when the number game is in a state that is unsafe to deploy (see bola.preflight)."""
    import os
    from app.core.rbac import bola_dev_only
    from app.services import bola
    with session_scope() as db:
        store = SettingsStore(db)
        active = sum(1 for row in bola.schedules(db) if row.is_active)
        results = bola.preflight(store, dev_only=bola_dev_only(),
                                 live_ack=os.environ.get("BOLA_LIVE_ACK") == "1",
                                 active_schedules=active)
    marks = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    for level, message in results:
        print(f"[{marks[level]}] bola: {message}")
    if any(level == "fail" for level, _ in results):
        sys.exit(1)


def unban_ip(args) -> None:
    """Recovery path if an IP ban ever shuts you out of the web panel. Prints what it lifted."""
    from app.db.models import IpBan
    from app.services import ipbans
    with session_scope() as db:
        rows = db.scalars(select(IpBan).where(IpBan.network == args.address.strip())).all()
        if not rows:
            print(f"No ban found for {args.address!r}. Current bans: "
                  f"{', '.join(r.network for r in ipbans.list_bans(db)) or 'none'}")
            return
        for row in rows:
            ipbans.unban(db, None, row.id)
            print(f"Lifted ban on {row.network}.")


def bola_tick(_args) -> None:
    from app.services import bola
    with session_scope() as db:
        store = SettingsStore(db)
        if not store.get("bola.enabled"):
            print("Number game is switched off (bola.enabled=false): nothing to do.")
            return
        out = bola.tick(db, store)
        print(f"Opened {out['opened']}, locked {out['locked']}.")


def streams_check(args) -> None:
    """Health-check the video sources that are in use. Run by the timer; --repeat/--interval make one run check
    several times (the timer fires every minute, so 2 x 30 s = a check about every 30 seconds)."""
    import time
    from app.services import streams
    for i in range(max(1, args.repeat)):
        with session_scope() as db:
            if not SettingsStore(db).get("stream.enabled"):
                print("The Streams page is switched off: nothing to check.")
                return
            changes = streams.check_all(db)
        for c in changes:
            print(f"{c['name']}: now {c['now']} ({c['why']})")
        if not changes:
            print("No change.")
        if i + 1 < args.repeat:
            time.sleep(max(1, args.interval))


def bola_convert_cuts(args) -> None:
    """CSV with header: username,com_percentage,sop_perc   (old values, e.g. 0.25 and 0.10)."""
    import csv
    pool = float(args.pool)
    rows, bad = [], []
    with open(args.rates, newline="") as fh:
        for rec in csv.DictReader(fh):
            name = (rec.get("username") or "").strip()
            try:
                vol, sop = convert_cut(float(rec["com_percentage"]), float(rec["sop_perc"]), pool)
                rows.append((name, float(rec["com_percentage"]), float(rec["sop_perc"]), vol, sop))
            except (ValueError, KeyError) as exc:
                bad.append((name, str(exc)))
    print(f"pool = {pool:.2f} of sales   (bola.commission.pool_bps should be {round(pool * 10000)})")
    print(f"{'username':<16}{'old com':>9}{'old sop':>9}{'volume_bps':>12}{'sop_bps':>9}")
    for name, com, sop, vol, sbps in rows:
        print(f"{name:<16}{com:>9.2f}{sop:>9.2f}{vol:>12}{sbps:>9}")
    for name, why in bad:
        print(f"SKIPPED {name}: {why}")
    if not args.apply:
        print("\nDry run. Re-run with --apply to write these to the Cuts of existing Agents (the old Cavos).")
        return
    with session_scope() as db:
        done = 0
        for name, _com, _sop, vol, sbps in rows:
            agent = db.scalar(select(User).where(User.username == name,
                                                 User.role == Role.AGENT))
            if agent is None:
                print(f"NOT FOUND (no such Agent): {name}")
                continue
            agent.cut_bps, agent.sop_bps = vol, sbps
            done += 1
        print(f"Updated {done} Agent(s).")


def bola_import_legacy(args) -> None:
    """Areas, draw times, the line limit and past results from the old dump. Dry run unless --apply."""
    from app.services import bola_legacy
    plan = bola_legacy.read_plan(args.sql)
    print(plan.summary())
    if not args.apply:
        print("\nDry run. Nothing was written. Re-run with --apply to create the above.")
        return
    with session_scope() as db:
        actor = db.scalar(select(User).where(User.role == Role.ADMIN).order_by(User.id))
        if actor is None:
            sys.exit("Error: create the Admin account first (create-admin).")
        made = bola_legacy.apply_plan(db, actor, plan, SettingsStore(db))
    print(f"\nCreated {made['areas']} area(s), {made['schedules']} draw time(s), {made['draws']} past draw(s). "
          "Passwords, credits, users and tickets are never imported.")


def refresh_reports(args) -> None:
    """Recompute the heavy report figures (systemd timer). Safe to run any time, any number of times."""
    from app.services import snapshots
    only = [x.strip() for x in (args.only or "").split(",") if x.strip()] or None
    unknown = [x for x in (only or []) if x not in snapshots.KINDS]
    if unknown:
        sys.exit(f"Unknown report(s): {', '.join(unknown)}. Choose from: {', '.join(snapshots.KINDS)}.")
    with session_scope() as db:
        done = snapshots.refresh_all(db, only)
    for name, snap in done.items():
        print(f"{name:<10} {snap.duration_ms:>6} ms")


def sqlite_to_postgres(args) -> None:
    """Copy a SQLite database into an empty PostgreSQL one (dry run unless --apply)."""
    from app.db.sqlite_to_postgres import MigrationError, migrate, redact
    source = args.source or (get_settings().DATABASE_URL if get_settings().is_sqlite else None)
    if not source:
        sys.exit("Error: --source sqlite:///path/to.db is required.")
    print(f"{redact(source)}  ->  {redact(args.target)}")
    try:
        migrate(source, args.target, apply=args.apply)
    except MigrationError as exc:
        sys.exit(f"Error: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db").set_defaults(fn=init_db)
    p = sub.add_parser("create-admin")
    p.add_argument("--username", required=True)
    p.add_argument("--full-name", default="Administrator")
    p.add_argument("--password", help="Prefer ADMIN_PASSWORD env or the prompt")
    p.set_defaults(fn=create_admin)
    m = sub.add_parser("create-master")
    m.add_argument("--username", required=True)
    m.add_argument("--full-name", default="Master")
    m.add_argument("--password", help="Prefer MASTER_PASSWORD env or the prompt")
    m.set_defaults(fn=create_master)
    pw = sub.add_parser("create-pawn")
    pw.add_argument("--username", required=True)
    pw.add_argument("--full-name", default="Pawn")
    pw.add_argument("--password", help="Prefer PAWN_PASSWORD env or the prompt")
    pw.set_defaults(fn=create_pawn)
    sub.add_parser("seed-demo").set_defaults(fn=seed_demo)
    sl = sub.add_parser("seed-locations")
    sl.add_argument("--no-events", action="store_true", help="do not attach venues to events that have none")
    sl.set_defaults(fn=seed_locations)
    sub.add_parser("gen-keys").set_defaults(fn=gen_keys)
    sub.add_parser("encrypt-pii").set_defaults(fn=encrypt_pii)
    bg = sub.add_parser("bola-generate")
    bg.add_argument("--date", help="YYYY-MM-DD (default: today, Manila)")
    bg.add_argument("--days", type=int, default=1)
    bg.set_defaults(fn=bola_generate)
    ub = sub.add_parser("unban-ip")
    ub.add_argument("address")
    ub.set_defaults(fn=unban_ip)
    rr = sub.add_parser("refresh-reports")
    rr.add_argument("--only", help="comma list of: flows, top, integrity (default: all)")
    rr.set_defaults(fn=refresh_reports)
    sp = sub.add_parser("sqlite-to-postgres")
    sp.add_argument("--source", help="sqlite:///./dev.db (default: DATABASE_URL when it is SQLite)")
    sp.add_argument("--target", required=True, help="postgresql+psycopg://user:pass@host/db (must be empty)")
    sp.add_argument("--apply", action="store_true", help="write; without it only the source is counted")
    sp.set_defaults(fn=sqlite_to_postgres)
    sub.add_parser("bola-tick").set_defaults(fn=bola_tick)
    sc = sub.add_parser("streams-check")
    sc.add_argument("--repeat", type=int, default=1, help="checks per run (default 1)")
    sc.add_argument("--interval", type=int, default=30, help="seconds between checks when repeating")
    sc.set_defaults(fn=streams_check)
    sub.add_parser("bola-preflight").set_defaults(fn=bola_preflight)
    bc = sub.add_parser("bola-convert-cuts")
    bc.add_argument("--rates", required=True, help="CSV: username,com_percentage,sop_perc")
    bc.add_argument("--pool", default="0.30", help="share of sales set aside (default 0.30)")
    bc.add_argument("--apply", action="store_true")
    bc.set_defaults(fn=bola_convert_cuts)
    bi = sub.add_parser("bola-import-legacy")
    bi.add_argument("--sql", required=True, help="the old panel's MySQL dump (online_bukis.sql)")
    bi.add_argument("--apply", action="store_true", help="write; without it this only prints the plan")
    bi.set_defaults(fn=bola_import_legacy)
    args = parser.parse_args()
    try:
        args.fn(args)
    except StelError as exc:
        sys.exit(f"Error: {exc.message}")


if __name__ == "__main__":
    main()
