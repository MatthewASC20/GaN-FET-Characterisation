"""SQLite schema declarations and row mapping.

Keeping schema evolution separate from query methods makes migrations easier
to review and prevents positional row contracts from being duplicated.
"""

from __future__ import annotations

import sqlite3

from gan_fet.core.models import FinalReadings, MatrixPoint, RunRecord

SCHEMA_VERSION = 9

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
    tuned_voltage_v REAL,
    vin REAL, iin REAL, fsw_hz REAL, irms REAL, vds_pk REAL, isw_rms REAL,
    screenshot_path TEXT,
    attempt_no INTEGER NOT NULL DEFAULT 1,
    tuned_frequency_hz REAL,
    tuned_input_power_w REAL,
    sweep_direction TEXT,
    zvs_dwell_fraction REAL
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
    detail TEXT,
    -- Trip context. A trip outside a run has no run_id to hang diagnosis on,
    -- so the instantaneous operating point is captured with the event itself.
    ctx_frequency_hz REAL,
    ctx_bus_setpoint_v REAL,
    ctx_vds_peak_v REAL,
    ctx_dc_current_a REAL
);

-- The applied test plan, from schema 7; ``completed_at`` from schema 8.
--
-- One plan, not a slot per table. Schema 7 had a second 'draft' slot holding
-- the planner's expanded cross-product, rewritten on every click: nothing ever
-- read it, and it stored ~60x the data of the selections it was derived from.
-- The planner's state now lives in plan_selections / plan_options below, and
-- what remains here is the single plan the rig is working from. A slot column
-- with one legal value is dead generality, and a bare string slot means a typo
-- yields a silently separate plan rather than an error.
--
-- Points are marked complete rather than deleted. The table still *drains* on
-- screen — Planned Tests shows the outstanding rows — but the plan survives
-- being finished, so it can be inspected afterwards or applied again to
-- another part. Deleting made "what was in that plan?" unanswerable.
--
-- Not derived by subtracting ``runs``: that cannot express a deliberate
-- re-test, whose points already have completed runs.
--
-- Deliberately not referencing devices(id): a plan is a statement of intent
-- about a named device, and deleting that device's runs should not silently
-- empty the plan that asked for them.
CREATE TABLE IF NOT EXISTS plan_points (
    position INTEGER PRIMARY KEY,
    device_name TEXT NOT NULL,
    config TEXT NOT NULL,
    frequency_hz INTEGER NOT NULL,
    duty_pct INTEGER NOT NULL,
    temperature_c INTEGER NOT NULL,
    voltage_v INTEGER NOT NULL,
    completed_at TEXT
);

-- A meta row with no points is impossible now that points are kept, but the
-- row still carries what the points cannot: where the plan came from and which
-- device it was built for. No meta row means no plan applied, which stays
-- distinct from a plan whose points are all complete. Conflating those two is
-- what made a finished plan render as a blank table.
CREATE TABLE IF NOT EXISTS plan_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    source TEXT NOT NULL,
    device_name TEXT NOT NULL,
    saved_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The planner's parameter selections, per device, from schema 8.
--
-- The selections rather than the matrix they expand to: 24 values instead of
-- 1500 rows, and the listing regenerates from them exactly. Going the other
-- way is not possible — an expanded cross-product cannot tell you which boxes
-- were ticked.
CREATE TABLE IF NOT EXISTS plan_selections (
    device_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (device_name, kind, value)
);

CREATE TABLE IF NOT EXISTS plan_options (
    device_name TEXT PRIMARY KEY,
    include_completed INTEGER NOT NULL DEFAULT 0,
    duration_minutes REAL NOT NULL DEFAULT 1.0,
    tune_voltage INTEGER NOT NULL DEFAULT 1
);
"""

#: Just the two applied-plan tables, for the schema 7 rebuild. Duplicated from
#: ``SCHEMA`` rather than parsed out of it, because a migration that silently
#: follows the *current* definition is a migration that changes meaning every
#: time the schema does.
PLAN_TABLES_V8 = """
CREATE TABLE plan_points (
    position INTEGER PRIMARY KEY,
    device_name TEXT NOT NULL,
    config TEXT NOT NULL,
    frequency_hz INTEGER NOT NULL,
    duty_pct INTEGER NOT NULL,
    temperature_c INTEGER NOT NULL,
    voltage_v INTEGER NOT NULL,
    completed_at TEXT
);
CREATE TABLE plan_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    source TEXT NOT NULL,
    device_name TEXT NOT NULL,
    saved_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

#: The planner's multi-select boxes, and the ``plan_selections.kind`` values
#: they are stored under. Names match ``PlannerTab.param_options``.
PLAN_SELECTION_KINDS = (
    "frequencies",
    "configurations",
    "duties",
    "voltages",
    "temperatures",
)

#: Columns added after the original table definitions, migrated in place with
#: ``ALTER TABLE ... ADD COLUMN``. SQLite makes that atomic and leaves existing
#: rows NULL, which is exactly the "not recorded" semantics wanted here.
ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "runs": (
        ("tuned_frequency_hz", "REAL"),
        ("tuned_input_power_w", "REAL"),
        ("sweep_direction", "TEXT"),
        # Recorded on every run from schema 6 so ordinary matrix work
        # accumulates the data needed to decide whether ZVS onset can be
        # bisected, without a dedicated bench session.
        ("zvs_dwell_fraction", "REAL"),
    ),
    "safety_events": (
        ("ctx_frequency_hz", "REAL"),
        ("ctx_bus_setpoint_v", "REAL"),
        ("ctx_vds_peak_v", "REAL"),
        ("ctx_dc_current_a", "REAL"),
    ),
}

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
    "r.bus_voltage_v, r.tuned_voltage_v, r.vin, r.iin, r.fsw_hz, r.irms, r.vds_pk, "
    "r.isw_rms, r.screenshot_path, r.attempt_no, "
    "r.tuned_frequency_hz, r.tuned_input_power_w, r.sweep_direction, "
    "r.zvs_dwell_fraction"
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
        tuned_voltage_v=row[12],
        readings=FinalReadings(
            vin=row[13],
            iin=row[14],
            fsw_hz=row[15],
            irms=row[16],
            vds_pk=row[17],
            isw_rms=row[18],
        ),
        screenshot_path=row[19],
        tuned_frequency_hz=row[21],
        tuned_input_power_w=row[22],
        sweep_direction=row[23],
        zvs_dwell_fraction=row[24],
    )
