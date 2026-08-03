from __future__ import annotations

import csv
import sqlite3
from dataclasses import replace
from pathlib import Path

from gan_fet.core.models import FinalReadings
from gan_fet.storage.export import export_device
from gan_fet.storage.db import Database
from gan_fet.storage.reports import generate_device_report


def test_attempts_are_append_only_and_completed_attempt_remains_preferred(
    database, matrix_point
):
    first = database.create_run(matrix_point, 1.0)
    database.complete_run(first, FinalReadings(vin=60.0, iin=0.1, fsw_hz=13e6))
    retry = database.create_run(matrix_point, 1.0)
    database.set_run_status(retry, "failed")

    attempts = database.run_attempts_for_point(matrix_point)
    preferred = database.find_run(matrix_point)

    assert [attempt.attempt_no for attempt in attempts] == [2, 1]
    assert preferred is not None
    assert preferred.id == first
    assert preferred.status == "completed"


def test_safety_event_read_api(database, matrix_point):
    run_id = database.create_run(matrix_point, 1.0)
    database.add_safety_event(run_id, "over-current", "1.2 A")

    events = database.safety_events(run_id)

    assert len(events) == 1
    assert events[0][1] == run_id
    assert events[0][3:] == ("over-current", "1.2 A")


def test_nested_transaction_uses_savepoint(database, matrix_point):
    with database.transaction():
        run_id = database.create_run(matrix_point, 1.0)
        try:
            with database.transaction():
                database.add_sample(run_id, "now", 0.1)
                raise RuntimeError("rollback inner")
        except RuntimeError:
            pass
        database.set_run_status(run_id, "failed")

    assert database.samples_for_run(run_id) == []
    assert database.get_run(run_id).status == "failed"


def test_export_includes_every_attempt_and_its_samples_in_history_order(
    database,
    matrix_point,
    tmp_path: Path,
):
    later_point = replace(matrix_point, voltage_v=300)
    later_id = database.create_run(later_point, 1.0)
    database.set_run_status(later_id, "failed")
    database.add_sample(later_id, "later", 0.30)

    attempt_ids: list[int] = []
    for attempt, status in enumerate(
        ("completed", "failed", "cancelled", "interrupted"),
        start=1,
    ):
        run_id = database.create_run(matrix_point, 1.0)
        attempt_ids.append(run_id)
        database.add_sample(run_id, f"attempt-{attempt}", attempt / 100)
        if status == "completed":
            database.complete_run(
                run_id,
                FinalReadings(vin=60.0, iin=0.1, fsw_hz=13e6),
            )
        else:
            database.set_run_status(run_id, status)

    all_attempts = database.all_run_attempts_for_device(
        matrix_point.device_name
    )
    preferred = database.runs_for_device(matrix_point.device_name)
    runs_path, samples_path = export_device(
        database,
        matrix_point.device_name,
        tmp_path / "out",
    )

    assert [
        (run.point.voltage_v, run.attempt_no, run.status)
        for run in all_attempts
    ] == [
        (200, 1, "completed"),
        (200, 2, "failed"),
        (200, 3, "cancelled"),
        (200, 4, "interrupted"),
        (300, 1, "failed"),
    ]
    assert [run.id for run in preferred] == [attempt_ids[0], later_id]

    with runs_path.open(newline="", encoding="utf-8") as stream:
        run_rows = list(csv.DictReader(stream))
    with samples_path.open(newline="", encoding="utf-8") as stream:
        sample_rows = list(csv.DictReader(stream))

    assert [
        (row["Voltage (V)"], row["Attempt"], row["Status"])
        for row in run_rows
    ] == [
        ("200", "1", "completed"),
        ("200", "2", "failed"),
        ("200", "3", "cancelled"),
        ("200", "4", "interrupted"),
        ("300", "1", "failed"),
    ]
    assert [int(row["Run ID"]) for row in run_rows] == [
        *attempt_ids,
        later_id,
    ]
    assert [int(row["Run ID"]) for row in sample_rows] == [
        *attempt_ids,
        later_id,
    ]


def test_simulation_exports_and_report_are_unmistakably_watermarked(
    database,
    matrix_point,
    tmp_path: Path,
):
    run_id = database.create_run(matrix_point, 0.1)
    database.add_sample(run_id, "now", 0.021, 66.7, 66.7, 1.7, 0.8, 0.0)
    database.complete_run(
        run_id,
        FinalReadings(
            vin=66.7,
            iin=0.021,
            fsw_hz=13_000_000,
            irms=1.7,
            vds_pk=200.0,
            isw_rms=0.8,
        ),
    )

    runs_path, samples_path = export_device(
        database,
        matrix_point.device_name,
        tmp_path / "exports",
        is_simulated=True,
    )
    report_path = generate_device_report(
        database,
        matrix_point.device_name,
        tmp_path / "reports",
        is_simulated=True,
    )

    assert "SIMULATION" in runs_path.name
    assert "SIMULATION" in samples_path.name
    assert "SIMULATION" in report_path.name
    with runs_path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["Data Origin"] == "SIMULATION — NOT MEASURED DATA"
    assert "SIMULATION — VIRTUAL INSTRUMENTS — NOT MEASURED DATA" in (
        report_path.read_text(encoding="utf-8")
    )


def test_newer_database_schema_is_rejected(tmp_path: Path):
    path = tmp_path / "project" / "future.db"
    path.parent.mkdir()
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO schema_version(version) VALUES (999)")
    connection.commit()
    connection.close()

    try:
        Database(path)
    except sqlite3.DatabaseError as exc:
        assert "newer" in str(exc)
    else:
        raise AssertionError("future schema should not open")


def test_unrelated_unique_index_survives_reopening(tmp_path: Path):
    path = tmp_path / "project" / "index.db"
    db = Database(path)
    db.close()
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE UNIQUE INDEX keep_unique_screenshot ON runs(screenshot_path)"
    )
    connection.commit()
    connection.close()

    reopened = Database(path)
    reopened.close()
    connection = sqlite3.connect(path)
    indexes = {
        row[1] for row in connection.execute("PRAGMA index_list(runs)").fetchall()
    }
    connection.close()

    assert "keep_unique_screenshot" in indexes


def test_v8_tuning_columns_are_renamed_in_place(tmp_path: Path):
    """A schema-8 database opens with v_zvs / find_zvs renamed, values intact.

    The rename must be a migration, not a rebuild: prior measurements and the
    operator's saved planner preference both survive, and reopening the
    migrated file must not attempt the rename again.
    """
    path = tmp_path / "v8.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO schema_version(version) VALUES (8);
        CREATE TABLE devices (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            param_options_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO devices(id, name) VALUES (1, 'DUT-1');
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY,
            device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            config TEXT NOT NULL,
            frequency_hz INTEGER NOT NULL,
            duty_pct INTEGER NOT NULL,
            temperature_c INTEGER NOT NULL,
            voltage_v INTEGER NOT NULL,
            duration_minutes REAL,
            started_at TEXT,
            completed_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            bus_voltage_v REAL,
            v_zvs REAL,
            vin REAL, iin REAL, fsw_hz REAL, irms REAL, vds_pk REAL,
            isw_rms REAL,
            screenshot_path TEXT,
            attempt_no INTEGER NOT NULL DEFAULT 1,
            tuned_frequency_hz REAL,
            tuned_input_power_w REAL,
            sweep_direction TEXT,
            zvs_dwell_fraction REAL
        );
        INSERT INTO runs(device_id, config, frequency_hz, duty_pct,
                         temperature_c, voltage_v, duration_minutes, status,
                         v_zvs)
        VALUES (1, 'Single Device', 13560000, 50, 25, 300, 1.0, 'completed',
                95.5);
        CREATE TABLE plan_options (
            device_name TEXT PRIMARY KEY,
            include_completed INTEGER NOT NULL DEFAULT 0,
            duration_minutes REAL NOT NULL DEFAULT 1.0,
            find_zvs INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO plan_options(device_name, include_completed,
                                 duration_minutes, find_zvs)
        VALUES ('DUT-1', 0, 2.5, 0);
        """
    )
    connection.commit()
    connection.close()

    for _reopen in range(2):
        db = Database(path)
        try:
            columns = {
                row[1]
                for row in db._conn.execute("PRAGMA table_info(runs)")
            }
            assert "tuned_voltage_v" in columns and "v_zvs" not in columns
            value = db._conn.execute(
                "SELECT tuned_voltage_v FROM runs"
            ).fetchone()[0]
            assert value == 95.5
            stored = db.load_plan_selections("DUT-1")
            assert stored is not None
            assert stored[2] == 2.5
            assert stored[3] is False
        finally:
            db.close()


def test_a_pre_attempts_database_is_refused_not_rebuilt(tmp_path: Path):
    """The one-row-per-point era predates every database still in service.

    The rebuild that used to converge it was retired; opening such a file
    must fail closed with a pointer to a release that still carries the
    migration, never silently query a table whose rows mean something else.
    """
    path = tmp_path / "ancient.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY,
            device_id INTEGER NOT NULL REFERENCES devices(id),
            config TEXT NOT NULL,
            frequency_hz INTEGER NOT NULL,
            duty_pct INTEGER NOT NULL,
            temperature_c INTEGER NOT NULL,
            voltage_v INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'running'
        );
        """
    )
    connection.commit()
    connection.close()

    try:
        Database(path)
    except sqlite3.DatabaseError as exc:
        assert "attempts migration" in str(exc)
    else:
        raise AssertionError("a pre-attempts database must be refused")
