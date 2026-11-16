"""Plan section 9: the concurrency tests that only mean something on PostgreSQL.

Skipped unless BOLA_PG_URL points at a DISPOSABLE PostgreSQL database, for example:

    BOLA_PG_URL=postgresql+psycopg://user:pass@127.0.0.1:5432/scratch python -m pytest tests/test_bola_postgres.py

It runs scripts/bola_loadtest.py in a subprocess (own interpreter, own engine) so the suite's
SQLite settings are untouched.
"""
import os
import pathlib
import subprocess
import sys

import pytest

URL = os.environ.get("BOLA_PG_URL", "")
ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not URL.startswith("postgresql"), reason="set BOLA_PG_URL to a scratch PostgreSQL")
def test_load_and_race_checks_on_postgres():
    env = {**os.environ, "DATABASE_URL": URL, "ENV": "dev", "COOKIE_SECURE": "false",
           "SECRET_KEY": "test-secret-key-test-secret-key-0123456789",
           "DB_POOL_SIZE": "40", "DB_MAX_OVERFLOW": "20"}
    run = subprocess.run([sys.executable, "scripts/bola_loadtest.py", "--bets", "300", "--rounds", "10"],
                         cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-1500:]
    assert "ALL CHECKS PASSED" in run.stdout


def test_load_script_refuses_sqlite():
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{ROOT}/var/never-used-loadtest.db", "ENV": "dev",
           "COOKIE_SECURE": "false", "SECRET_KEY": "test-secret-key-test-secret-key-0123456789"}
    run = subprocess.run([sys.executable, "scripts/bola_loadtest.py"], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=120)
    assert run.returncode == 2 and "Refusing to run on SQLite" in run.stdout
