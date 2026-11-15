"""Operations CLI.

    python -m app.cli init-db
    python -m app.cli create-admin --username admin --full-name "Site Admin"
    python -m app.cli seed-demo          # dev only: sample agents, station, event
    python -m app.cli gen-keys           # prints SECRET_KEY and PII_KEY values
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
from app.db.models import Role, Station, Status, User
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
    print("Database tables and system accounts are ready.")


def create_admin(args) -> None:
    password = args.password or os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Password: ")
    with session_scope() as db:
        store = SettingsStore(db)
        if db.scalar(select(User.id).where(User.username == args.username)):
            sys.exit(f"User '{args.username}' already exists.")
        problems = password_problems(password, args.username)
        if len(password) < int(store.get("security.password_min_length")):
            problems.append("Password is shorter than the configured minimum.")
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
        teller = agents.create_agent(db, admin, username="demo_teller", password=demo_pw,
                                     role=Role.TELLER, full_name="Demo Teller", settings=store)
        if not db.scalar(select(Station).where(Station.code == "STATION01")):
            station = Station(code="STATION01", name="Demo Station", user_id=teller.id, is_active=True)
            db.add(station)
            db.flush()
            accounts.cash_for_station(db, station)
        players.register_card_player(db, admin, card_number="0000000001", full_name="Demo Player",
                                     birth_date=dt.date(1990, 1, 1), settings=store,
                                     agent_id=master.id)
        events.create_event(db, admin, title="Demo Event", event_date=dt.date.today(),
                            settings=store, fight_count=5)
    print(f"Demo data created. Demo users share the password: {demo_pw}")


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


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db").set_defaults(fn=init_db)
    p = sub.add_parser("create-admin")
    p.add_argument("--username", required=True)
    p.add_argument("--full-name", default="Administrator")
    p.add_argument("--password", help="Prefer ADMIN_PASSWORD env or the prompt")
    p.set_defaults(fn=create_admin)
    sub.add_parser("seed-demo").set_defaults(fn=seed_demo)
    sub.add_parser("gen-keys").set_defaults(fn=gen_keys)
    sub.add_parser("encrypt-pii").set_defaults(fn=encrypt_pii)
    args = parser.parse_args()
    try:
        args.fn(args)
    except StelError as exc:
        sys.exit(f"Error: {exc.message}")


if __name__ == "__main__":
    main()
