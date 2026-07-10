"""Storage-layer tests: legacy migration against the real repo data tree,
DB queries, and the Sheets cell-mapping helpers (pure functions)."""

from pathlib import Path

import pytest

from gan_fet.core.autotune import tuned_frequency_search_order
from gan_fet.core.models import MatrixPoint
from gan_fet.sheets.sync import column_letter, _cell
from gan_fet.storage.db import Database
from gan_fet.storage.migrate import migrate_legacy_tree

LEGACY_TREE = Path(__file__).resolve().parent.parent / "Device Data"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.mark.skipif(not LEGACY_TREE.is_dir(), reason="legacy Device Data tree not present")
def test_migration_of_real_tree(db):
    report = migrate_legacy_tree(db, LEGACY_TREE)
    assert report.runs_imported >= 560
    assert report.samples_imported > 10_000
    assert len(report.devices) == 7

    # Spot check against the raw FINAL_READINGS row of a known file:
    # FINAL_READINGS,117.239863,2.67148785511,0.035257025,6700000.0
    point = MatrixPoint("145D2", "Single Conduction", 6_000_000, 25, 25, 300)
    run = db.find_run(point)
    assert run is not None and run.status == "completed"
    assert run.readings.vin == pytest.approx(117.239863)
    assert run.readings.irms == pytest.approx(2.67148785511)
    assert run.readings.iin == pytest.approx(0.035257025)
    assert run.readings.fsw_hz == pytest.approx(6_700_000.0)

    # Idempotent: a second pass imports nothing new.
    report2 = migrate_legacy_tree(db, LEGACY_TREE)
    assert report2.runs_imported == 0
    assert report2.runs_skipped_existing == report.runs_imported


def test_prior_tuned_frequency_search_order():
    configs = ["Dual Conduction", "Single Conduction", "Single Device"]
    order = tuned_frequency_search_order(80, "Single Conduction", configs)
    assert order[0] == (80, "Single Conduction")
    assert (80, "Dual Conduction") in order[:3]
    assert order[3:][0][0] == 25  # 25 °C fallbacks come after same-temp options
    # at 25 °C there is no duplicate fallback block
    assert len(tuned_frequency_search_order(25, "Single Conduction", configs)) == 3


def test_run_lifecycle_and_completed_points(db):
    point = MatrixPoint("DEV", "Single Device", 6_000_000, 25, 40, 200)
    run_id = db.create_run(point, duration_minutes=1.0)
    assert db.completed_points("DEV", 6_000_000) == set()

    from gan_fet.core.models import FinalReadings

    db.complete_run(run_id, FinalReadings(iin=0.01, fsw_hz=6_100_000.0))
    assert db.completed_points("DEV", 6_000_000) == {("Single Device", 25, 200, 40)}

    # one result per matrix point: a new run replaces the old one
    # (note: SQLite may reuse the rowid — identity is the matrix point)
    db.add_sample(run_id, "2026-01-01 00:00:00", 0.01)
    new_id = db.create_run(point, duration_minutes=2.0)
    replacement = db.get_run(new_id)
    assert replacement.status == "running"
    assert replacement.duration_minutes == 2.0
    assert db.samples_for_run(new_id) == []  # old run's samples cascaded away
    assert db.completed_points("DEV", 6_000_000) == set()

    # prior tuned frequency comes from the completed run only
    db.complete_run(new_id, FinalReadings(fsw_hz=6_200_000.0))
    found = db.prior_tuned_frequency("DEV", 6_000_000, 25, 200, [(40, "Single Device")])
    assert found == (6_200_000.0, "Single Device", 40)


def test_column_letter_and_cell_mapping():
    assert column_letter(1) == "A"
    assert column_letter(26) == "Z"
    assert column_letter(27) == "AA"
    # Dual Conduction vin is column 1, duty 25 / 300 V is row 5
    assert _cell("25°C", "Dual Conduction", 25, 300, "vin") == "25°C!A5"
    # Single Device irms is column 27 → AA
    assert _cell("40°C", "Single Device", 50, 400, "irms") == "40°C!AA13"
    assert _cell("25°C", "Single Conduction", 25, 200, "isw") is None
