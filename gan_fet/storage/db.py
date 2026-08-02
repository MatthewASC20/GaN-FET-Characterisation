"""SQLite storage — the single source of truth for all experiment data.

Thread-safe: a single connection guarded by a lock (the UI thread, the
experiment engine thread and the Sheets sync thread all use the same
Database instance).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

from gan_fet.core.models import (
    FinalReadings,
    MatrixPoint,
    RunRecord,
    TripContext,
)
from gan_fet.storage.schema import (
    ADDED_COLUMNS,
    RUN_COLUMNS,
    RUN_NATURAL_KEY_COLUMNS,
    RUNS_WITHOUT_LEGACY_UNIQUE,
    SCHEMA,
    SCHEMA_VERSION,
    row_to_run,
)

log = logging.getLogger(__name__)

class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)

        from gan_fet.storage.network_lock import NetworkProjectLock

        self._net_lock = NetworkProjectLock(self.path.parent)
        self._lock = threading.RLock()
        self._transaction_depth = 0
        self._savepoint_counter = 0
        self._closed = False

        self._net_lock.acquire()
        try:
            self._net_lock.assert_owned()
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.execute("PRAGMA busy_timeout=10000")
            journal_mode = self._conn.execute(
                "PRAGMA journal_mode=DELETE"
            ).fetchone()[0]
            if str(journal_mode).lower() != "delete":
                raise sqlite3.OperationalError(
                    f"could not enable network-safe DELETE journal mode: {journal_mode}"
                )
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._initialize_schema()
        except Exception:
            conn = getattr(self, "_conn", None)
            if conn is not None:
                conn.close()
            self._net_lock.release()
            raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._conn.close()
            finally:
                self._closed = True
                self._net_lock.release()

    def _initialize_schema(self) -> None:
        """Create the current schema and migrate the original one-row-per-point table.

        Structural inspection is deliberate: early builds had no version row, so
        trusting only ``schema_version`` would strand those databases.
        """
        self._net_lock.assert_owned()
        version_table = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if version_table is not None:
            existing_version = self._conn.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
            if existing_version is not None and int(existing_version) > SCHEMA_VERSION:
                raise sqlite3.DatabaseError(
                    f"database schema version {existing_version} is newer than "
                    f"this application supports ({SCHEMA_VERSION})"
                )
        with self._conn:
            self._conn.executescript(SCHEMA)
            self._net_lock.assert_owned()

        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        # Only the historical natural-key UNIQUE forces one-row-per-point
        # semantics. Unrelated administrative unique indexes must not trigger
        # a destructive table rebuild.
        unique_point_index = False
        for index_row in self._conn.execute("PRAGMA index_list(runs)").fetchall():
            if not bool(index_row[2]):
                continue
            index_name = str(index_row[1]).replace("'", "''")
            index_columns = tuple(
                row[2]
                for row in self._conn.execute(
                    f"PRAGMA index_info('{index_name}')"
                ).fetchall()
            )
            if index_columns == RUN_NATURAL_KEY_COLUMNS:
                unique_point_index = True
                break
        if "attempt_no" not in columns or unique_point_index:
            self._migrate_runs_to_attempts()

        sample_columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(samples)").fetchall()
        }
        if "elapsed_s" not in sample_columns:
            self._migrate_samples_add_elapsed()

        self._migrate_added_columns()

        with self._conn:
            # Early migration builds marked every parseable legacy CSV complete,
            # even when no FINAL_READINGS row existed. Those imports have a
            # zero duration and no final metric; preserve their samples while
            # making the incomplete state explicit.
            self._conn.execute(
                """
                UPDATE runs SET status='legacy_partial'
                WHERE status='completed'
                  AND duration_minutes=0.0
                  AND vin IS NULL AND iin IS NULL AND fsw_hz IS NULL
                  AND irms IS NULL AND vds_pk IS NULL AND isw_rms IS NULL
                """
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            self._net_lock.assert_owned()

    def _migrate_added_columns(self) -> None:
        """Add later nullable columns in place, idempotently.

        ``ALTER TABLE ... ADD COLUMN`` is atomic in SQLite and leaves existing
        rows NULL, which is the correct "was never recorded" value for both the
        tuning result and the trip context. Re-running this is a no-op, so a
        database at any prior version converges without a rebuild.
        """
        self._net_lock.assert_owned()
        for table, additions in ADDED_COLUMNS.items():
            existing = {
                row[1]
                for row in self._conn.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            missing = [
                (name, sql_type)
                for name, sql_type in additions
                if name not in existing
            ]
            if not missing:
                continue
            with self._conn:
                for name, sql_type in missing:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"
                    )
                self._net_lock.assert_owned()
            log.info(
                "migrated %s: added %s",
                table,
                ", ".join(name for name, _ in missing),
            )

    def _migrate_samples_add_elapsed(self) -> None:
        """Add nullable active elapsed time without rewriting legacy samples."""
        self._net_lock.assert_owned()
        with self._conn:
            # SQLite's ADD COLUMN is atomic and leaves every existing row NULL,
            # which distinguishes legacy wall-clock-only data from new samples.
            self._conn.execute("ALTER TABLE samples ADD COLUMN elapsed_s REAL")
            self._net_lock.assert_owned()

    def _migrate_runs_to_attempts(self) -> None:
        """Idempotently rebuild ``runs`` without the legacy natural-key UNIQUE."""
        self._conn.commit()
        self._net_lock.assert_owned()
        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute("DROP TABLE IF EXISTS runs_v2")
            self._conn.execute(RUNS_WITHOUT_LEGACY_UNIQUE)
            self._conn.execute(
                """
                INSERT INTO runs_v2(
                    id, device_id, config, frequency_hz, duty_pct, temperature_c,
                    voltage_v, duration_minutes, started_at, completed_at, status,
                    bus_voltage_v, v_zvs, vin, iin, fsw_hz, irms, vds_pk, isw_rms,
                    screenshot_path, attempt_no
                )
                SELECT
                    id, device_id, config, frequency_hz, duty_pct, temperature_c,
                    voltage_v, duration_minutes, started_at, completed_at, status,
                    bus_voltage_v, v_zvs, vin, iin, fsw_hz, irms, vds_pk, isw_rms,
                    screenshot_path, 1
                FROM runs
                """
            )
            self._conn.execute("DROP TABLE runs")
            self._conn.execute("ALTER TABLE runs_v2 RENAME TO runs")
            self._conn.execute(
                """
                CREATE INDEX idx_runs_point
                ON runs(
                    device_id, config, frequency_hz, duty_pct,
                    temperature_c, voltage_v, id
                )
                """
            )
            self._conn.execute("CREATE INDEX idx_runs_status ON runs(status)")
            self._net_lock.assert_owned()
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")

        violations = self._conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"foreign-key violations after runs migration: {violations[:5]}"
            )

    @contextmanager
    def transaction(self):
        """Thread-safe transaction with savepoint-backed nesting.

        Database helpers called inside this context participate in it and never
        commit independently.
        """
        with self._lock:
            if self._transaction_depth == 0:
                self._net_lock.assert_owned()
                self._conn.execute("BEGIN IMMEDIATE")
                self._transaction_depth = 1
                try:
                    yield self._conn
                    self._net_lock.assert_owned()
                except Exception:
                    self._conn.rollback()
                    raise
                else:
                    self._conn.commit()
                finally:
                    self._transaction_depth = 0
                return

            self._savepoint_counter += 1
            self._net_lock.assert_owned()
            savepoint = f"gan_fet_sp_{self._savepoint_counter}"
            self._conn.execute(f"SAVEPOINT {savepoint}")
            self._transaction_depth += 1
            try:
                yield self._conn
                self._net_lock.assert_owned()
            except Exception:
                self._conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            else:
                self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            finally:
                self._transaction_depth -= 1

    @contextmanager
    def _write_scope(self):
        """Commit a standalone write, or join the caller's active transaction."""
        with self._lock:
            if self._transaction_depth:
                yield
                self._net_lock.assert_owned()
            else:
                with self._conn:
                    self._net_lock.assert_owned()
                    yield
                    self._net_lock.assert_owned()

    def backup(self, target_path: Path | str) -> None:
        """Perform an online hot backup of the SQLite database."""
        dest = Path(target_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._net_lock.assert_owned()
            backup_conn = sqlite3.connect(dest)
            try:
                self._conn.backup(backup_conn)
                self._net_lock.assert_owned()
            finally:
                backup_conn.close()

    def get_summary_stats(self) -> dict[str, int]:
        """Return counts for devices, total runs, completed runs, and total samples."""
        with self._lock:
            devices_cnt = self._conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
            total_runs = self._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            completed_runs = self._conn.execute("SELECT COUNT(*) FROM runs WHERE status='completed'").fetchone()[0]
            samples_cnt = self._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        return {
            "devices": devices_cnt,
            "total_runs": total_runs,
            "completed_runs": completed_runs,
            "total_samples": samples_cnt,
        }

    def _execute(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._write_scope():
            return self._conn.execute(sql, tuple(args))

    # -- devices -----------------------------------------------------

    def get_or_create_device(self, name: str) -> int:
        name = name.strip()
        if not name:
            raise ValueError("device name must not be empty")
        with self._write_scope():
            return self._get_or_create_device_locked(name)

    def _get_or_create_device_locked(self, name: str) -> int:
        row = self._conn.execute(
            "SELECT id FROM devices WHERE name = ?", (name,)
        ).fetchone()
        if row:
            return int(row[0])
        cur = self._conn.execute(
            "INSERT INTO devices(name) VALUES (?)", (name,)
        )
        if cur.lastrowid is None:
            raise sqlite3.DatabaseError("device insert did not return an id")
        return int(cur.lastrowid)

    def list_devices(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM devices ORDER BY name"
            ).fetchall()
        return [r[0] for r in rows]

    def delete_device(self, name: str) -> bool:
        """Delete a device and all associated runs and samples via CASCADE."""
        name = name.strip()
        with self._write_scope():
            cur = self._conn.execute("DELETE FROM devices WHERE name = ?", (name,))
            return cur.rowcount > 0

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
        payload = json.dumps(options)
        name = name.strip()
        if not name:
            raise ValueError("device name must not be empty")
        with self._write_scope():
            device_id = self._get_or_create_device_locked(name)
            self._conn.execute(
                "UPDATE devices SET param_options_json = ? WHERE id = ?",
                (payload, device_id),
            )

    # -- runs ----------------------------------------------------------

    def find_run(self, point: MatrixPoint) -> Optional[RunRecord]:
        """Return the preferred attempt for a point.

        A completed attempt remains authoritative when a newer retry fails or
        is cancelled. If the point has never completed, return its latest
        attempt so callers can still inspect current/partial state.
        """
        with self._lock:
            row = self._conn.execute(
                f"SELECT {RUN_COLUMNS} FROM runs r JOIN devices d ON d.id = r.device_id"
                " WHERE d.name=? AND r.config=? AND r.frequency_hz=? AND r.duty_pct=?"
                " AND r.temperature_c=? AND r.voltage_v=?"
                " ORDER BY CASE WHEN r.status='completed' THEN 0 ELSE 1 END, r.id DESC"
                " LIMIT 1",
                (point.device_name, point.config, point.frequency_hz,
                 point.duty_pct, point.temperature_c, point.voltage_v),
            ).fetchone()
        return row_to_run(row) if row else None

    def create_run(
        self,
        point: MatrixPoint,
        duration_minutes: float,
        *,
        status: str = "running",
        started_at: Optional[str] = None,
        replace_existing: bool = True,
    ) -> int:
        """Append a run attempt without deleting prior measurements.

        ``replace_existing`` is retained for API compatibility. Replacements
        are now logical: selectors prefer the newest successful attempt while
        all attempts remain available for audit and recovery.
        """
        del replace_existing
        with self._write_scope():
            name = point.device_name.strip()
            if not name:
                raise ValueError("device name must not be empty")
            device_id = self._get_or_create_device_locked(name)
            point_args = (
                device_id, point.config, point.frequency_hz, point.duty_pct,
                point.temperature_c, point.voltage_v,
            )
            attempt_no = self._conn.execute(
                """
                SELECT COALESCE(MAX(attempt_no), 0) + 1
                FROM runs
                WHERE device_id=? AND config=? AND frequency_hz=? AND duty_pct=?
                  AND temperature_c=? AND voltage_v=?
                """,
                point_args,
            ).fetchone()[0]
            cur = self._conn.execute(
                "INSERT INTO runs(device_id, config, frequency_hz, duty_pct,"
                " temperature_c, voltage_v, duration_minutes, started_at, status,"
                " attempt_no) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (device_id, point.config, point.frequency_hz, point.duty_pct,
                 point.temperature_c, point.voltage_v, duration_minutes,
                 started_at or time.strftime("%Y-%m-%d %H:%M:%S"), status,
                 attempt_no),
            )
            if cur.lastrowid is None:
                raise sqlite3.DatabaseError("run insert did not return an id")
            return int(cur.lastrowid)

    def complete_run(
        self,
        run_id: int,
        readings: FinalReadings,
        *,
        bus_voltage_v: Optional[float] = None,
        v_zvs: Optional[float] = None,
        screenshot_path: Optional[str] = None,
        completed_at: Optional[str] = None,
        tuned_frequency_hz: Optional[float] = None,
        tuned_input_power_w: Optional[float] = None,
        sweep_direction: Optional[str] = None,
        zvs_dwell_fraction: Optional[float] = None,
    ) -> None:
        self._execute(
            "UPDATE runs SET status='completed', completed_at=?, bus_voltage_v=?,"
            " v_zvs=?, vin=?, iin=?, fsw_hz=?, irms=?, vds_pk=?, isw_rms=?,"
            " screenshot_path=COALESCE(?, screenshot_path),"
            " tuned_frequency_hz=?, tuned_input_power_w=?, sweep_direction=?,"
            " zvs_dwell_fraction=?"
            " WHERE id=?",
            (completed_at or time.strftime("%Y-%m-%d %H:%M:%S"), bus_voltage_v,
             v_zvs, readings.vin, readings.iin, readings.fsw_hz, readings.irms,
             readings.vds_pk, readings.isw_rms, screenshot_path,
             tuned_frequency_hz, tuned_input_power_w, sweep_direction,
             zvs_dwell_fraction, run_id),
        )

    def set_run_status(self, run_id: int, status: str) -> None:
        terminal_statuses = {
            "completed",
            "failed",
            "cancelled",
            "tripped",
            "interrupted",
            "legacy_partial",
        }
        if status in terminal_statuses:
            self._execute(
                """
                UPDATE runs
                SET status=?, completed_at=COALESCE(completed_at, ?)
                WHERE id=?
                """,
                (status, time.strftime("%Y-%m-%d %H:%M:%S"), run_id),
            )
        else:
            self._execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))

    def recover_interrupted_runs(self) -> int:
        """Mark orphaned running attempts as interrupted.

        Callers should invoke this explicitly after they have established that
        no experiment worker from the previous application session is still
        active.  Database construction deliberately does not perform recovery,
        because opening a database for inspection must not mutate run state.
        """
        completed_at = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._write_scope():
            cursor = self._conn.execute(
                """
                UPDATE runs
                SET status='interrupted', completed_at=?
                WHERE status='running'
                """,
                (completed_at,),
            )
            return cursor.rowcount

    def set_run_screenshot(self, run_id: int, screenshot_path: str) -> None:
        self._execute(
            "UPDATE runs SET screenshot_path=? WHERE id=?", (screenshot_path, run_id)
        )

    def delete_run(self, run_id: int) -> None:
        """Delete all attempts for the selected point.

        This preserves the historical UI meaning of "Delete Test"; without it,
        deleting the preferred attempt would unexpectedly reveal an older one.
        """
        with self._write_scope():
            row = self._conn.execute(
                """
                SELECT device_id, config, frequency_hz, duty_pct, temperature_c, voltage_v
                FROM runs WHERE id=?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                return
            self._conn.execute(
                """
                DELETE FROM runs
                WHERE device_id=? AND config=? AND frequency_hz=? AND duty_pct=?
                  AND temperature_c=? AND voltage_v=?
                """,
                tuple(row),
            )

    def delete_run_attempt(self, run_id: int) -> None:
        """Delete one attempt; primarily useful for administrative repair."""
        self._execute("DELETE FROM runs WHERE id=?", (run_id,))

    def get_run(self, run_id: int) -> Optional[RunRecord]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {RUN_COLUMNS} FROM runs r JOIN devices d ON d.id=r.device_id"
                " WHERE r.id=?",
                (run_id,),
            ).fetchone()
        return row_to_run(row) if row else None

    def runs_for_device(self, device_name: str) -> list[RunRecord]:
        """One preferred attempt per matrix point for reports and current UI."""
        with self._lock:
            rows = self._conn.execute(
                f"""
                WITH ranked AS (
                    SELECT r.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY r.device_id, r.config, r.frequency_hz,
                                            r.duty_pct, r.temperature_c, r.voltage_v
                               ORDER BY CASE WHEN r.status='completed' THEN 0 ELSE 1 END,
                                        r.id DESC
                           ) AS preferred_rank
                    FROM runs r
                    JOIN devices selected_device ON selected_device.id=r.device_id
                    WHERE selected_device.name=?
                )
                SELECT {RUN_COLUMNS}
                FROM ranked r JOIN devices d ON d.id=r.device_id
                WHERE r.preferred_rank=1
                ORDER BY r.frequency_hz, r.temperature_c, r.config,
                         r.duty_pct, r.voltage_v
                """,
                (device_name,),
            ).fetchall()
        return [row_to_run(r) for r in rows]

    def all_run_attempts_for_device(self, device_name: str) -> list[RunRecord]:
        """Every append-only attempt in deterministic point/history order.

        This is the lossless query for export and audit workflows.  Preferred
        selectors intentionally remain separate so reports and the current UI
        continue to show one authoritative attempt per matrix point.
        """
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT {RUN_COLUMNS}
                FROM runs r JOIN devices d ON d.id=r.device_id
                WHERE d.name=?
                ORDER BY r.frequency_hz, r.temperature_c, r.config,
                         r.duty_pct, r.voltage_v, r.attempt_no, r.id
                """,
                (device_name,),
            ).fetchall()
        return [row_to_run(row) for row in rows]

    def run_attempts_for_point(self, point: MatrixPoint) -> list[RunRecord]:
        """All attempts for a point, newest first."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {RUN_COLUMNS} FROM runs r JOIN devices d ON d.id=r.device_id"
                " WHERE d.name=? AND r.config=? AND r.frequency_hz=? AND r.duty_pct=?"
                " AND r.temperature_c=? AND r.voltage_v=? ORDER BY r.id DESC",
                (
                    point.device_name, point.config, point.frequency_hz,
                    point.duty_pct, point.temperature_c, point.voltage_v,
                ),
            ).fetchall()
        return [row_to_run(r) for r in rows]

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
                    " AND r.status='completed' AND r.fsw_hz IS NOT NULL"
                    " ORDER BY r.id DESC LIMIT 1",
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
        elapsed_s: Optional[float] = None,
    ) -> None:
        self._execute(
            "INSERT INTO samples(run_id, ts, dc_current, smu_voltage, dc_voltage,"
            " rms_current, isw_rms, elapsed_s) VALUES (?,?,?,?,?,?,?,?)",
            (
                run_id,
                ts,
                dc_current,
                smu_voltage,
                dc_voltage,
                rms_current,
                isw_rms,
                elapsed_s,
            ),
        )

    def add_samples(self, run_id: int, rows: list[tuple]) -> None:
        """Bulk-import legacy rows, whose active elapsed time is unknown.

        Each row is ``(ts, dc_current, smu_voltage, dc_voltage, rms_current,
        isw_rms)``. The omitted ``elapsed_s`` column intentionally remains NULL.
        """
        with self._write_scope():
            self._conn.executemany(
                "INSERT INTO samples(run_id, ts, dc_current, smu_voltage,"
                " dc_voltage, rms_current, isw_rms) VALUES (?,?,?,?,?,?,?)",
                [(run_id, *r) for r in rows],
            )

    def samples_for_run(self, run_id: int) -> list[tuple]:
        with self._lock:
            return self._conn.execute(
                "SELECT ts, dc_current, smu_voltage, dc_voltage, rms_current,"
                " isw_rms, elapsed_s FROM samples WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()

    # -- safety ---------------------------------------------------------

    def add_safety_event(
        self,
        run_id: Optional[int],
        kind: str,
        detail: str,
        context: Optional[TripContext] = None,
    ) -> None:
        """Record a safety event, with the operating point where possible.

        A trip during manual bench work has no ``run_id`` to hang diagnosis on,
        so the instantaneous frequency, bus setpoint, Vds peak and DC current
        are stored alongside the event itself. Any of them may be ``None`` —
        telemetry is often exactly what has just failed.
        """
        self._execute(
            "INSERT INTO safety_events("
            "run_id, kind, detail, ctx_frequency_hz, ctx_bus_setpoint_v, "
            "ctx_vds_peak_v, ctx_dc_current_a) VALUES (?,?,?,?,?,?,?)",
            (
                run_id,
                kind,
                detail,
                None if context is None else context.frequency_hz,
                None if context is None else context.bus_setpoint_v,
                None if context is None else context.vds_peak_v,
                None if context is None else context.dc_current_a,
            ),
        )

    def latest_safety_event_id(self) -> int:
        """Return an inexpensive watermark for generation-safe association."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM safety_events"
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def associate_latest_unassigned_safety_event(
        self,
        run_id: int,
        kind: str = "estop",
        *,
        after_id: int = 0,
    ) -> bool:
        """Attach the newest matching standalone event created after a watermark.

        An E-stop can be audited just before an experiment worker creates its
        run row.  ``after_id`` identifies that engine invocation, preventing
        an older standalone event from being attached to the new run.
        """
        if run_id <= 0:
            raise ValueError("run_id must be positive")
        if not kind:
            raise ValueError("safety event kind must not be empty")
        if after_id < 0:
            raise ValueError("after_id must not be negative")
        with self._write_scope():
            cursor = self._conn.execute(
                """
                UPDATE safety_events
                SET run_id=?
                WHERE id=(
                    SELECT id
                    FROM safety_events
                    WHERE run_id IS NULL AND kind=? AND id>?
                    ORDER BY id DESC
                    LIMIT 1
                )
                  AND run_id IS NULL
                """,
                (run_id, kind, after_id),
            )
            return cursor.rowcount == 1

    def safety_events(
        self, run_id: Optional[int] = None
    ) -> list[tuple[int, Optional[int], str, str, Optional[str]]]:
        """Return recorded safety events, newest first.

        Keeping this read API beside ``add_safety_event`` makes the audit log
        available to diagnostics and restores the public behavior used by the
        original simulated-rig tests.
        """
        sql = "SELECT id, run_id, ts, kind, detail FROM safety_events"
        args: tuple[Any, ...] = ()
        if run_id is not None:
            sql += " WHERE run_id=?"
            args = (run_id,)
        sql += " ORDER BY id DESC"
        with self._lock:
            return [tuple(row) for row in self._conn.execute(sql, args).fetchall()]
