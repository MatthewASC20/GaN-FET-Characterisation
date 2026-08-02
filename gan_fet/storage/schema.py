"""SQLite schema declarations and row mapping.

Keeping schema evolution separate from query methods makes migrations easier
to review and prevents positional row contracts from being duplicated.
"""

from __future__ import annotations

import sqlite3

from gan_fet.core.models import FinalReadings, MatrixPoint, RunRecord

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    param_options_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS runs (
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
    vin REAL, iin REAL, fsw_hz REAL, irms REAL, vds_pk REAL, isw_rms REAL,
    screenshot_path TEXT,
    attempt_no INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_runs_point
    ON runs(device_id, config, frequency_hz, duty_pct, temperature_c, voltage_v, id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    elapsed_s REAL,
    dc_current REAL,
    smu_voltage REAL,
    dc_voltage REAL,
    rms_current REAL,
    isw_rms REAL
);
CREATE INDEX IF NOT EXISTS idx_samples_run ON samples(run_id);

CREATE TABLE IF NOT EXISTS safety_events (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    kind TEXT NOT NULL,
    detail TEXT
);
"""

RUNS_WITHOUT_LEGACY_UNIQUE = """
CREATE TABLE runs_v2 (
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
    vin REAL, iin REAL, fsw_hz REAL, irms REAL, vds_pk REAL, isw_rms REAL,
    screenshot_path TEXT,
    attempt_no INTEGER NOT NULL DEFAULT 1
)
"""

RUN_COLUMNS = (
    "r.id, d.name, r.config, r.frequency_hz, r.duty_pct, r.temperature_c, "
    "r.voltage_v, r.duration_minutes, r.started_at, r.completed_at, r.status, "
    "r.bus_voltage_v, r.v_zvs, r.vin, r.iin, r.fsw_hz, r.irms, r.vds_pk, "
    "r.isw_rms, r.screenshot_path, r.attempt_no"
)

RUN_NATURAL_KEY_COLUMNS = (
    "device_id",
    "config",
    "frequency_hz",
    "duty_pct",
    "temperature_c",
    "voltage_v",
)


def row_to_run(row: sqlite3.Row | tuple) -> RunRecord:
    """Map the canonical ``RUN_COLUMNS`` projection to a domain record."""
    return RunRecord(
        id=row[0],
        point=MatrixPoint(
            device_name=row[1],
            config=row[2],
            frequency_hz=row[3],
            duty_pct=row[4],
            temperature_c=row[5],
            voltage_v=row[6],
        ),
        duration_minutes=row[7],
        started_at=row[8],
        completed_at=row[9],
        status=row[10],
        attempt_no=row[20],
        bus_voltage_v=row[11],
        v_zvs=row[12],
        readings=FinalReadings(
            vin=row[13],
            iin=row[14],
            fsw_hz=row[15],
            irms=row[16],
            vds_pk=row[17],
            isw_rms=row[18],
        ),
        screenshot_path=row[19],
    )
