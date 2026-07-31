"""SQLite storage — the single source of truth for all experiment data.

Thread-safe: a single connection guarded by a lock (the UI thread, the
experiment engine thread and the Sheets sync thread all use the same
Database instance).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from gan_fet.core.models import FinalReadings, MatrixPoint, RunRecord

_SCHEMA = """
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
    UNIQUE(device_id, config, frequency_hz, duty_pct, temperature_c, voltage_v)
);

CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
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

# Selected by name, mapped by name: adding a column to `runs` cannot silently
# shift what `_row_to_run` reads.
_RUN_COLUMNS = (
    "r.id, d.name AS device_name, r.config, r.frequency_hz, r.duty_pct, "
    "r.temperature_c, r.voltage_v, r.duration_minutes, r.started_at, "
    "r.completed_at, r.status, r.bus_voltage_v, r.v_zvs, r.vin, r.iin, "
    "r.fsw_hz, r.irms, r.vds_pk, r.isw_rms, r.screenshot_path"
)

_RUN_SELECT = (
    f"SELECT {_RUN_COLUMNS} FROM runs r JOIN devices d ON d.id = r.device_id"
)

#: The matrix-point columns, in the order the UNIQUE constraint uses them.
_POINT_FIELDS = (
    "config", "frequency_hz", "duty_pct", "temperature_c", "voltage_v",
)


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        id=row["id"],
        point=MatrixPoint(
            device_name=row["device_name"],
            config=row["config"],
            frequency_hz=row["frequency_hz"],
            duty_pct=row["duty_pct"],
            temperature_c=row["temperature_c"],
            voltage_v=row["voltage_v"],
        ),
        duration_minutes=row["duration_minutes"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        status=row["status"],
        bus_voltage_v=row["bus_voltage_v"],
        v_zvs=row["v_zvs"],
        readings=FinalReadings(
            vin=row["vin"], iin=row["iin"], fsw_hz=row["fsw_hz"],
            irms=row["irms"], vds_pk=row["vds_pk"], isw_rms=row["isw_rms"],
        ),
        screenshot_path=row["screenshot_path"],
    )


def _point_values(point: MatrixPoint) -> tuple:
    return tuple(getattr(point, field) for field in _POINT_FIELDS)


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row  # name-based column access
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _execute(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, tuple(args))

    # -- devices -----------------------------------------------------

    def get_or_create_device(self, name: str) -> int:
        name = name.strip()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT id FROM devices WHERE name = ?", (name,)
            ).fetchone()
            if row:
                return row[0]
            cur = self._conn.execute(
                "INSERT INTO devices(name) VALUES (?)", (name,)
            )
            return cur.lastrowid

    def list_devices(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM devices ORDER BY name"
            ).fetchall()
        return [r[0] for r in rows]

    def get_device_options(self, name: str) -> Optional[dict[str, list]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT param_options_json FROM devices WHERE name = ?", (name.strip(),)
            ).fetchone()
        if not row or not row[0]:
            return None
        try:
            data = json.loads(row[0])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None

    def set_device_options(self, name: str, options: dict[str, list]) -> None:
        device_id = self.get_or_create_device(name)
        self._execute(
            "UPDATE devices SET param_options_json = ? WHERE id = ?",
            (json.dumps(options), device_id),
        )

    # -- runs ----------------------------------------------------------

    def find_run(self, point: MatrixPoint) -> Optional[RunRecord]:
        with self._lock:
            row = self._conn.execute(
                f"{_RUN_SELECT} WHERE d.name=? AND r.config=? AND r.frequency_hz=?"
                " AND r.duty_pct=? AND r.temperature_c=? AND r.voltage_v=?",
                (point.device_name, *_point_values(point)),
            ).fetchone()
        return _row_to_run(row) if row else None

    def create_run(
        self,
        point: MatrixPoint,
        duration_minutes: float,
        *,
        status: str = "running",
        started_at: Optional[str] = None,
        replace_existing: bool = True,
    ) -> int:
        device_id = self.get_or_create_device(point.device_name)
        with self._lock, self._conn:
            if replace_existing:
                self._conn.execute(
                    "DELETE FROM runs WHERE device_id=? AND config=? AND frequency_hz=?"
                    " AND duty_pct=? AND temperature_c=? AND voltage_v=?",
                    (device_id, *_point_values(point)),
                )
            cur = self._conn.execute(
                "INSERT INTO runs(device_id, config, frequency_hz, duty_pct,"
                " temperature_c, voltage_v, duration_minutes, started_at, status)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (device_id, point.config, point.frequency_hz, point.duty_pct,
                 point.temperature_c, point.voltage_v, duration_minutes,
                 started_at or time.strftime("%Y-%m-%d %H:%M:%S"), status),
            )
            return cur.lastrowid

    def complete_run(
        self,
        run_id: int,
        readings: FinalReadings,
        *,
        bus_voltage_v: Optional[float] = None,
        v_zvs: Optional[float] = None,
        screenshot_path: Optional[str] = None,
        completed_at: Optional[str] = None,
    ) -> None:
        self._execute(
            "UPDATE runs SET status='completed', completed_at=?, bus_voltage_v=?,"
            " v_zvs=?, vin=?, iin=?, fsw_hz=?, irms=?, vds_pk=?, isw_rms=?,"
            " screenshot_path=COALESCE(?, screenshot_path) WHERE id=?",
            (completed_at or time.strftime("%Y-%m-%d %H:%M:%S"), bus_voltage_v,
             v_zvs, readings.vin, readings.iin, readings.fsw_hz, readings.irms,
             readings.vds_pk, readings.isw_rms, screenshot_path, run_id),
        )

    def set_run_status(self, run_id: int, status: str) -> None:
        self._execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))

    def set_run_screenshot(self, run_id: int, screenshot_path: str) -> None:
        self._execute(
            "UPDATE runs SET screenshot_path=? WHERE id=?", (screenshot_path, run_id)
        )

    def delete_run(self, run_id: int) -> None:
        self._execute("DELETE FROM runs WHERE id=?", (run_id,))

    def get_run(self, run_id: int) -> Optional[RunRecord]:
        with self._lock:
            row = self._conn.execute(
                f"{_RUN_SELECT} WHERE r.id=?", (run_id,)
            ).fetchone()
        return _row_to_run(row) if row else None

    def runs_for_device(self, device_name: str) -> list[RunRecord]:
        with self._lock:
            rows = self._conn.execute(
                f"{_RUN_SELECT} WHERE d.name=? ORDER BY r.frequency_hz,"
                " r.temperature_c, r.config, r.duty_pct, r.voltage_v",
                (device_name,),
            ).fetchall()
        return [_row_to_run(r) for r in rows]

    def completed_points(
        self, device_name: str, frequency_hz: Optional[int] = None
    ) -> set[tuple[str, int, int, int]]:
        """Set of (config, duty, voltage, temperature) with a completed run."""
        sql = (
            "SELECT r.config, r.duty_pct, r.voltage_v, r.temperature_c"
            " FROM runs r JOIN devices d ON d.id=r.device_id"
            " WHERE d.name=? AND r.status='completed'"
        )
        args: list[Any] = [device_name]
        if frequency_hz is not None:
            sql += " AND r.frequency_hz=?"
            args.append(frequency_hz)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return {tuple(r) for r in rows}

    def prior_tuned_frequency(
        self,
        device_name: str,
        frequency_hz: int,
        duty_pct: int,
        voltage_v: int,
        search_order: list[tuple[int, str]],
    ) -> Optional[tuple[float, str, int]]:
        """First completed run matching (temp, config) in search_order with a
        measured switching frequency. Returns (fsw_hz, config, temp)."""
        for temp, config in search_order:
            with self._lock:
                row = self._conn.execute(
                    "SELECT r.fsw_hz FROM runs r JOIN devices d ON d.id=r.device_id"
                    " WHERE d.name=? AND r.frequency_hz=? AND r.duty_pct=?"
                    " AND r.voltage_v=? AND r.temperature_c=? AND r.config=?"
                    " AND r.status='completed' AND r.fsw_hz IS NOT NULL",
                    (device_name, frequency_hz, duty_pct, voltage_v, temp, config),
                ).fetchone()
            if row and row[0]:
                return float(row[0]), config, temp
        return None

    # -- samples -------------------------------------------------------

    def add_sample(
        self,
        run_id: int,
        ts: str,
        dc_current: Optional[float],
        smu_voltage: Optional[float] = None,
        dc_voltage: Optional[float] = None,
        rms_current: Optional[float] = None,
        isw_rms: Optional[float] = None,
    ) -> None:
        self._execute(
            "INSERT INTO samples(run_id, ts, dc_current, smu_voltage, dc_voltage,"
            " rms_current, isw_rms) VALUES (?,?,?,?,?,?,?)",
            (run_id, ts, dc_current, smu_voltage, dc_voltage, rms_current, isw_rms),
        )

    def add_samples(self, run_id: int, rows: list[tuple]) -> None:
        """Bulk insert of (ts, dc_current, smu_voltage, dc_voltage, rms_current, isw_rms)."""
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO samples(run_id, ts, dc_current, smu_voltage,"
                " dc_voltage, rms_current, isw_rms) VALUES (?,?,?,?,?,?,?)",
                [(run_id, *r) for r in rows],
            )

    def samples_for_run(self, run_id: int) -> list[tuple]:
        with self._lock:
            return self._conn.execute(
                "SELECT ts, dc_current, smu_voltage, dc_voltage, rms_current,"
                " isw_rms FROM samples WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()

    # -- safety ---------------------------------------------------------

    def add_safety_event(self, run_id: Optional[int], kind: str, detail: str) -> None:
        self._execute(
            "INSERT INTO safety_events(run_id, kind, detail) VALUES (?,?,?)",
            (run_id, kind, detail),
        )

    def safety_events(
        self, run_id: Optional[int] = None
    ) -> list[tuple[str, str, str]]:
        """(timestamp, kind, detail) newest first, optionally for one run."""
        sql = "SELECT ts, kind, detail FROM safety_events"
        args: tuple = ()
        if run_id is not None:
            sql += " WHERE run_id=?"
            args = (run_id,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [(r["ts"], r["kind"], r["detail"]) for r in rows]
