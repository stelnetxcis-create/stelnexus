"""Operations for the number game: the pre-deploy check and the timer-driven CLI commands."""
import argparse
import re
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app import cli
from app.core.config import get_settings
from app.core.settings_registry import SettingsStore
from app.db.models import BolaDraw, User
from app.db.session import session_scope
from app.services import bola

ROOT = Path(__file__).resolve().parent.parent


class FakeSettings:
    """Just enough of SettingsStore for bola.preflight."""
    def __init__(self, **values):
        self.values = {"bola.enabled": True, "ticket.header": "StelNexus",
                       "bola.max_liability_per_outcome_cents": 100_000,
                       "bola.max_liability_per_draw_cents": 500_000,
                       "bola.show_odds_to_players": True, **values}

    def get(self, key):
        return self.values[key]


def run(dev_only, live_ack=False, schedules=3, **values):
    return bola.preflight(FakeSettings(**values), dev_only=dev_only, live_ack=live_ack,
                          active_schedules=schedules)


def levels(results):
    return [level for level, _ in results]


def test_off_passes_and_warns_if_the_dev_gate_is_already_open():
    assert levels(run(True, **{"bola.enabled": False})) == ["ok"]
    assert levels(run(False, **{"bola.enabled": False})) == ["ok", "warn"]


def test_dev_only_passes_even_without_caps_or_ack():
    out = run(True, live_ack=False, **{"bola.max_liability_per_outcome_cents": 0})
    assert "fail" not in levels(out)


def test_live_needs_every_safeguard():
    assert "fail" not in levels(run(False, live_ack=True))
    must_fail = (
        dict(live_ack=False),
        dict(live_ack=True, **{"bola.max_liability_per_outcome_cents": 0}),
        dict(live_ack=True, **{"bola.max_liability_per_draw_cents": 0}),
        dict(live_ack=True, **{"bola.show_odds_to_players": False}),
    )
    for kwargs in must_fail:
        assert "fail" in levels(run(False, **kwargs)), kwargs


@pytest.mark.parametrize("header", ["PCSO Bola", "Official Ticket", "pagcor games"])
@pytest.mark.parametrize("dev_only", [True, False])
def test_government_wording_in_the_ticket_header_always_fails(header, dev_only):
    assert "fail" in levels(run(dev_only, live_ack=True, **{"ticket.header": header}))


def test_no_schedules_is_a_warning_not_a_failure():
    out = run(True, schedules=0)
    assert "warn" in levels(out) and "fail" not in levels(out)


@pytest.fixture()
def switch():
    """Set bola.enabled for one test and put the previous value back (the DB is shared)."""
    def _set(value):
        with session_scope() as db:
            admin = db.scalar(select(User).where(User.username == "admin"))
            SettingsStore(db).set("bola.enabled", "true" if value else "false", admin.id)
    with session_scope() as db:
        before = bool(SettingsStore(db).get("bola.enabled"))
    yield _set
    _set(before)


def _draw_count():
    with session_scope() as db:
        return db.scalar(select(func.count(BolaDraw.id)))


def test_generate_and_tick_do_nothing_while_the_game_is_off(switch, capsys):
    switch(False)
    before = _draw_count()
    cli.bola_generate(argparse.Namespace(date=None, days=2))
    cli.bola_tick(argparse.Namespace())
    assert _draw_count() == before
    assert capsys.readouterr().out.count("switched off") == 2


def test_preflight_command_exits_nonzero_on_a_failure(switch, monkeypatch, capsys):
    switch(True)
    monkeypatch.setattr(get_settings(), "BOLA_DEV_ONLY", False)
    monkeypatch.delenv("BOLA_LIVE_ACK", raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.bola_preflight(argparse.Namespace())
    assert exc.value.code == 1
    assert "[FAIL]" in capsys.readouterr().out


def test_deploy_units_run_the_cli_commands_that_exist():
    """A typo in a unit file would fail silently at 3am; check each ExecStart names a real command."""
    commands = set(re.findall(r'add_parser\("([a-z-]+)"', (ROOT / "app/cli.py").read_text()))
    for name in ("tick", "generate"):
        service = (ROOT / f"deploy/stelnexus-bola-{name}.service").read_text()
        timer = (ROOT / f"deploy/stelnexus-bola-{name}.timer").read_text()
        exec_line = re.search(r"^ExecStart=.*-m app\.cli (\S+)", service, re.M)
        assert exec_line and exec_line.group(1) in commands, name
        assert "OnCalendar=" in timer and "[Install]" in timer
    assert "bola-preflight" in (ROOT / "scripts/security_check.sh").read_text()
