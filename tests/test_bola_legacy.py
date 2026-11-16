"""bola-import-legacy: parse a MySQL dump, plan, dry-run, apply (idempotent)."""
import datetime as dt
import subprocess
import sys

from sqlalchemy import select

from app.core.settings_registry import SettingsStore
from app.db.models import AppSetting, BolaDraw, BolaSchedule, Location, User
from app.db.session import session_scope
from app.services import bola_legacy

TAG = f"{dt.datetime.now().timestamp():.0f}"[-6:]

DUMP = f"""
-- MySQL dump (synthetic, for tests)
DROP TABLE IF EXISTS `tbl_area`;
CREATE TABLE `tbl_area` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `area` varchar(50) NOT NULL,
  `ball_1` time NOT NULL,
  `ball_2` time NOT NULL,
  `ball_3` time NOT NULL,
  PRIMARY KEY (`id`),
  KEY `area` (`area`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO `tbl_area` VALUES (1,'Legacy Laguna {TAG}','11:00:00','16:00:00','21:00:00'),
(2,'Legacy O''Brien Isabela {TAG}','11:00:00','00:00:00','21:00:00'),
(3,'Legacy Laguna {TAG}','09:00:00','00:00:00','00:00:00'),
(4,'Legacy Tarlac; (odd) {TAG}','12:00:00','00:00:00','18:30:00');

CREATE TABLE `tbl_settings` (
  `id` int(11) NOT NULL,
  `limit_amount_per_combi` decimal(10,2) DEFAULT NULL,
  PRIMARY KEY (`id`)
);
INSERT INTO `tbl_settings` VALUES (1,1500.00);

CREATE TABLE `tbl_event` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `id_area` int(11) DEFAULT NULL,
  `title` varchar(80) DEFAULT NULL,
  `ball_1` int(11) DEFAULT NULL,
  `ball_2` int(11) DEFAULT NULL,
  `ball_sched` int(11) DEFAULT NULL,
  `date_created` datetime DEFAULT NULL,
  `status` int(11) DEFAULT NULL,
  PRIMARY KEY (`id`)
);
INSERT INTO `tbl_event` (`id`,`id_area`,`title`,`ball_1`,`ball_2`,`ball_sched`,`date_created`,`status`) VALUES
(10,1,'Laguna 11am \\'draw\\'',12,24,1,'2024-03-01 11:05:00',1),
(11,1,'second same slot',3,4,1,'2024-03-01 11:07:00',1),
(12,1,'never ran',NULL,NULL,2,'2024-03-01 16:00:00',1),
(13,2,'dup balls',5,5,1,'2024-03-02 11:00:00',1),
(14,2,'ok',40,1,NULL,'2024-03-02 21:00:00',1),
(15,99,'unknown area',7,8,1,'2024-03-02 21:00:00',1);
"""


def test_parser_handles_escapes_nulls_parens_and_missing_column_lists():
    tables = bola_legacy.parse_dump(DUMP)
    assert tables["tbl_area"].columns == ["id", "area", "ball_1", "ball_2", "ball_3"]
    names = [r["area"] for r in tables["tbl_area"].rows]
    assert "Legacy O'Brien Isabela " + TAG in names and f"Legacy Tarlac; (odd) {TAG}" in names
    events = tables["tbl_event"].rows
    assert len(events) == 6 and events[0]["title"] == "Laguna 11am 'draw'"
    assert events[2]["ball_1"] is None and events[0]["ball_1"] == "12"
    assert tables["tbl_settings"].rows[0]["limit_amount_per_combi"] == "1500.00"


def test_plan_skips_no_draw_times_duplicates_and_events_that_never_ran():
    plan = bola_legacy.build_plan(bola_legacy.parse_dump(DUMP))
    by = {a.name: a for a in plan.areas}
    assert len(plan.areas) == 2                                        # duplicate Laguna dropped; "Tarlac;" not allowed
    assert plan.skipped["areas whose name the app does not allow"] == 1
    assert any("Tarlac" in w for w in plan.warnings)
    assert sorted(by[f"Legacy Laguna {TAG}"].times) == [1, 2, 3]
    assert sorted(by["Legacy O'Brien Isabela " + TAG].times) == [1, 3]   # 00:00 slot 2 = no draw
    assert plan.limit_per_line_cents == 150000
    got = {(d.area_name, d.day.isoformat(), d.slot, d.first, d.second) for d in plan.draws}
    # the two Laguna draws asked for slot 1: the second one is moved to the next free slot
    assert (f"Legacy Laguna {TAG}", "2024-03-01", 1, 12, 24) in got
    assert (f"Legacy Laguna {TAG}", "2024-03-01", 2, 3, 4) in got
    assert ("Legacy O'Brien Isabela " + TAG, "2024-03-02", 1, 40, 1) in got   # no ball_sched -> first free
    assert len(plan.draws) == 3
    assert plan.skipped["events that never ran (no valid balls)"] == 2     # NULL balls + equal balls
    assert plan.skipped["events with no usable date or area"] == 1


def test_missing_columns_are_reported_not_guessed():
    odd = "CREATE TABLE `tbl_area` (`id` int, `foo` varchar(9));\nINSERT INTO `tbl_area` VALUES (1,'x');"
    plan = bola_legacy.build_plan(bola_legacy.parse_dump(odd))
    assert plan.areas == [] and any("recognisable name column" in w for w in plan.warnings)
    assert any("Found: id, foo" in w for w in plan.warnings)
    empty = bola_legacy.build_plan(bola_legacy.parse_dump("-- nothing here"))
    assert empty.areas == [] and empty.warnings


def test_apply_creates_once_and_is_safe_to_rerun():
    plan = bola_legacy.build_plan(bola_legacy.parse_dump(DUMP))
    with session_scope() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        store = SettingsStore(db)
        first = bola_legacy.apply_plan(db, admin, plan, store)
        again = bola_legacy.apply_plan(db, admin, plan, store)
        assert first == {"areas": 2, "schedules": 5, "draws": 3}
        assert again == {"areas": 0, "schedules": 0, "draws": 0}
        loc = db.scalar(select(Location).where(Location.name == f"Legacy Laguna {TAG}"))
        assert len(db.scalars(select(BolaSchedule).where(BolaSchedule.location_id == loc.id)).all()) == 3
        draw = db.scalar(select(BolaDraw).where(BolaDraw.location_id == loc.id, BolaDraw.slot == 1))
        assert draw.legacy and str(draw.status) == "settled" and draw.result_label == "12 / 24"
        assert draw.pool_account_id is None                               # no escrow, no money, no tickets
        assert store.get("bola.limit_per_line_cents") == 150000
        for l in db.scalars(select(Location).where(Location.name.like(f"Legacy %{TAG}"))).all():
            l.is_active = False                                           # keep later fixtures clean


def test_cli_dry_run_writes_nothing(tmp_path):
    f = tmp_path / "dump.sql"
    f.write_text(DUMP.replace(TAG, "dryrun"))
    out = subprocess.run([sys.executable, "-m", "app.cli", "bola-import-legacy", "--sql", str(f)],
                         capture_output=True, text=True, cwd=".")
    assert out.returncode == 0, out.stderr
    assert "Dry run. Nothing was written" in out.stdout and "past draws:       3" in out.stdout
    assert "Legacy Laguna dryrun" in out.stdout
