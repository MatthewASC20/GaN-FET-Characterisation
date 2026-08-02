"""Optional, best-effort mirror of completed runs to Google Sheets.

Design rules:
* completely optional — the app runs, measures and stores without it;
* lazy — no authentication until the first item is processed;
* isolated — a background worker drains a queue; failures are reported to
  the status callback and never block or fail a run.

Cell layout matches the master spreadsheet template (one tab per
temperature, columns per rig configuration, rows per duty/voltage pair).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from gan_fet.core.models import MatrixPoint, RunRecord
from gan_fet.settings import GoogleSettings
from gan_fet.sheets import drive

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

# Template layout (from the original config.py): 1-based column per metric.
SHEET_MAPPINGS: dict[str, dict[str, int]] = {
    "Dual Conduction": {"vin": 1, "iin": 2, "fsw": 5, "irms": 9, "vds_pk": 4, "isw": 12},
    "Single Conduction": {"vin": 13, "iin": 14, "fsw": 17, "irms": 18, "vds_pk": 16},
    "Single Device": {"vin": 22, "iin": 23, "fsw": 26, "irms": 27, "vds_pk": 25},
}
DUTY_ROW_MAP: dict[int, dict[int, int]] = {
    25: {200: 4, 300: 5, 400: 6},
    50: {200: 11, 300: 12, 400: 13},
}
SYNC_RETRY_DELAYS_S = (0.25, 1.0)
_STOP = None


class UnmappedSheetPoint(ValueError):
    """The fixed spreadsheet template has no cell for a matrix point."""


def column_letter(index: int) -> str:
    """1-based column index → spreadsheet letters (1 → A, 27 → AA)."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _cell(sheet: str, config: str, duty: int, voltage: int, key: str) -> Optional[str]:
    columns = SHEET_MAPPINGS.get(config)
    rows = DUTY_ROW_MAP.get(duty)
    if not columns or key not in columns or not rows:
        return None
    row = rows.get(voltage)
    if row is None:
        return None
    return f"{sheet}!{column_letter(columns[key])}{row}"


class SheetsSync:
    def __init__(
        self,
        settings: GoogleSettings,
        frequencies_hz: list[int],
        status: Callable[[str], None] = lambda _msg: None,
    ):
        self.settings = settings
        self.frequencies_hz = frequencies_hz
        self.status = status
        self._queue: "queue.Queue[tuple[str, object] | None]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._state_lock = threading.Lock()
        self._closed = False
        self._sheets_service = None
        self._drive_service = None

    # -- availability ------------------------------------------------------

    @property
    def available(self) -> bool:
        if not self.settings.enabled:
            return False
        if not Path(self.settings.credentials_file).is_file():
            return False
        try:
            import googleapiclient  # noqa: F401
            import google.oauth2  # noqa: F401
        except ImportError:
            return False
        return True

    # -- public API (called from any thread; returns immediately) ----------

    def enqueue_run(self, record: RunRecord) -> None:
        if self.available:
            self._submit(("run", record))

    def enqueue_device_setup(self, device_name: str) -> None:
        if self.available:
            self._submit(("device", device_name))

    def enqueue_clear(self, point: MatrixPoint) -> None:
        if self.available:
            self._submit(("clear", point))

    def _submit(self, item: tuple[str, object]) -> None:
        closed = False
        with self._state_lock:
            if self._closed:
                closed = True
            else:
                self._queue.put(item)
                if self._worker is None:
                    self._worker = threading.Thread(
                        target=self._drain, daemon=True, name="sheets-sync"
                    )
                    self._worker.start()
        if closed:
            self._notify("Sheets sync is closed; update was not queued.")

    def close(self, *, flush: bool = True, timeout: float | None = None) -> bool:
        """Stop the worker, optionally draining all queued operations first.

        Returns ``True`` when the worker stopped within ``timeout``. The method
        is idempotent and enqueues exactly one sentinel.
        """
        with self._state_lock:
            if not self._closed:
                self._closed = True
                if not flush:
                    while True:
                        try:
                            pending = self._queue.get_nowait()
                        except queue.Empty:
                            break
                        else:
                            self._queue.task_done()
                            if pending is _STOP:
                                break
                if self._worker is not None:
                    self._queue.put(_STOP)
            worker = self._worker
        if worker is None:
            return True
        worker.join(timeout=timeout)
        return not worker.is_alive()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the current worker; useful for coordinated application exit."""
        with self._state_lock:
            worker = self._worker
        if worker is None:
            return True
        worker.join(timeout=timeout)
        return not worker.is_alive()

    # -- worker ----------------------------------------------------------

    def _services(self):
        if self._sheets_service is None:
            from google.oauth2.service_account import Credentials
            from googleapiclient.discovery import build

            creds = Credentials.from_service_account_file(
                self.settings.credentials_file, scopes=SCOPES
            )
            self._sheets_service = build("sheets", "v4", credentials=creds)
            self._drive_service = build("drive", "v3", credentials=creds)
        return self._sheets_service, self._drive_service

    def _drain(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                kind, payload = item  # type: ignore[misc]
                if kind == "run":
                    self._sync_run(payload)          # type: ignore[arg-type]
                elif kind == "device":
                    self._setup_device(payload)      # type: ignore[arg-type]
                elif kind == "clear":
                    self._clear_point(payload)       # type: ignore[arg-type]
            except Exception as exc:
                log.warning("Sheets sync (%s) failed: %s", kind, exc)
                self._notify(f"Sheets sync failed: {exc}")
            finally:
                self._queue.task_done()

    def _notify(self, message: str) -> None:
        try:
            self.status(message)
        except Exception:
            log.exception("Sheets status callback failed")

    @staticmethod
    def _execute_with_retry(operation, description: str):
        last_error: Exception | None = None
        for attempt in range(len(SYNC_RETRY_DELAYS_S) + 1):
            try:
                return operation().execute()
            except Exception as exc:
                last_error = exc
                if attempt >= len(SYNC_RETRY_DELAYS_S):
                    break
                time.sleep(SYNC_RETRY_DELAYS_S[attempt])
        assert last_error is not None
        raise RuntimeError(
            f"{description} failed after {len(SYNC_RETRY_DELAYS_S) + 1} attempts"
        ) from last_error

    def _spreadsheet_id(self, device_name: str, frequency_hz: int) -> Optional[str]:
        key = drive.mapping_key(device_name, frequency_hz)
        mappings = drive.load_mappings(self.settings)
        if key not in mappings:
            _, drive_service = self._services()
            mappings = drive.ensure_device_setup(
                drive_service, self.settings, device_name, [frequency_hz]
            )
        return mappings.get(key)

    def _setup_device(self, device_name: str) -> None:
        _, drive_service = self._services()
        drive.ensure_device_setup(
            drive_service, self.settings, device_name, self.frequencies_hz
        )
        self._notify(f"Drive setup complete for {device_name}.")

    @staticmethod
    def _cells_for_point(point: MatrixPoint) -> dict[str, str]:
        columns = SHEET_MAPPINGS.get(point.config)
        if not columns:
            raise UnmappedSheetPoint(
                f"configuration {point.config!r} is not present in the Sheet template"
            )
        rows = DUTY_ROW_MAP.get(point.duty_pct)
        if rows is None:
            raise UnmappedSheetPoint(
                f"duty {point.duty_pct}% is not present in the Sheet template"
            )
        if point.voltage_v not in rows:
            raise UnmappedSheetPoint(
                f"voltage {point.voltage_v} V is not present in the Sheet template"
            )

        sheet = f"{point.temperature_c}°C"
        cells = {
            key: _cell(
                sheet, point.config, point.duty_pct, point.voltage_v, key
            )
            for key in columns
        }
        if any(cell is None for cell in cells.values()):
            raise UnmappedSheetPoint(f"no complete Sheet mapping for {point.describe()}")
        return {key: cell for key, cell in cells.items() if cell is not None}

    def _sync_run(self, record: RunRecord) -> None:
        point = record.point
        spreadsheet_id = self._spreadsheet_id(point.device_name, point.frequency_hz)
        if not spreadsheet_id:
            self._notify(f"No spreadsheet mapping for {point.device_name}.")
            return

        sheets_service, _ = self._services()
        cells = self._cells_for_point(point)
        r = record.readings
        values = {
            "vin": r.vin,
            "iin": r.iin,
            "fsw": r.fsw_hz / 1e6 if r.fsw_hz is not None else None,
            "irms": r.irms,
            "vds_pk": r.vds_pk,
            "isw": r.isw_rms,
        }
        data = [
            {
                "range": cell,
                # Empty string deliberately replaces stale values from a prior
                # successful attempt when the new reading is unavailable.
                "values": [[values.get(key) if values.get(key) is not None else ""]],
            }
            for key, cell in cells.items()
        ]
        self._execute_with_retry(
            lambda: sheets_service.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "RAW", "data": data},
            ),
            f"Sheet update for {point.describe()}",
        )
        log.info("Synced run %s to Google Sheets", point.describe())

    def _clear_point(self, point: MatrixPoint) -> None:
        spreadsheet_id = self._spreadsheet_id(point.device_name, point.frequency_hz)
        if not spreadsheet_id:
            return
        sheets_service, _ = self._services()
        ranges = list(self._cells_for_point(point).values())
        self._execute_with_retry(
            lambda: sheets_service.spreadsheets().values().batchClear(
                spreadsheetId=spreadsheet_id,
                body={"ranges": ranges},
            ),
            f"Sheet clear for {point.describe()}",
        )
        log.info("Cleared sheet cells for %s", point.describe())
