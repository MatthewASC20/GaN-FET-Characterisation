"""Composition root and CLI entry point.

    gan-fet             launch the GUI
    gan-fet --migrate   import the legacy ``Device Data/`` CSV tree, then exit
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from gan_fet.settings import LEGACY_DATA_ROOT, Settings, SettingsLoadError
from gan_fet.storage.db import Database
from gan_fet.storage.migrate import migrate_legacy_tree

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from gan_fet.core.command_logger import ScpiFileLogger
    from gan_fet.sheets.sync import SheetsSync


def _build_instruments(settings: Settings, simulate: bool = False):
    """Compatibility wrapper around the application-facing rig builder."""
    from gan_fet.instruments.rig import build_instrument_rig

    return build_instrument_rig(settings, simulate=simulate).legacy_tuple()


class _ApplicationResources:
    """Idempotent owner for every process-scoped resource."""

    def __init__(self) -> None:
        self.db: Optional[Database] = None
        self.sheets: Optional[SheetsSync] = None
        self.scpi_logger: Optional[ScpiFileLogger] = None
        self.clients: list[Any] = []
        self._closed = False
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True

        if self.sheets is not None:
            try:
                if not self.sheets.close(flush=True, timeout=15.0):
                    log.warning("Sheets worker did not stop before timeout")
            except Exception:
                log.exception("Could not close Sheets sync")

        for client in self.clients:
            try:
                client.close()
            except Exception:
                log.exception("Could not close instrument client")

        if self.scpi_logger is not None:
            try:
                if not self.scpi_logger.close(flush=True, timeout=15.0):
                    log.warning("SCPI logger did not stop before timeout")
            except Exception:
                log.exception("Could not close SCPI logger")

        if self.db is not None:
            try:
                self.db.close()
            except Exception:
                log.exception("Could not close database")


def _instrument_clients(*instruments) -> list[Any]:
    clients: list[Any] = []
    seen: set[int] = set()
    for instrument in instruments:
        client = getattr(instrument, "client", None)
        if client is not None and id(client) not in seen:
            clients.append(client)
            seen.add(id(client))
    return clients


def _open_runtime_database(path: Path) -> tuple[Database, int]:
    """Open the DB and make interrupted prior attempts auditable before use."""
    db = Database(path)
    try:
        recovered = db.recover_interrupted_runs()
    except Exception:
        db.close()
        raise
    if recovered:
        log.warning(
            "Recovered %d interrupted run(s) from the previous session",
            recovered,
        )
    return db, recovered


def _maybe_offer_migration(db: Database, root) -> None:
    """On first launch with an empty DB, offer to import the legacy tree."""
    if db.list_devices() or not LEGACY_DATA_ROOT.is_dir():
        return
    from tkinter import messagebox

    if messagebox.askyesno(
        "Import Legacy Data",
        "The database is empty, but a legacy 'Device Data' folder was found.\n\n"
        "Import all previous experiment results now?",
        parent=root,
    ):
        report = migrate_legacy_tree(db, LEGACY_DATA_ROOT)
        messagebox.showinfo("Import Complete", report.summary(), parent=root)


def _fast_instrument_probe(settings: Settings) -> bool:
    """Compatibility boolean for the structured transport-aware preflight."""
    from gan_fet.instruments.discovery import probe_configured_instruments

    report = probe_configured_instruments(settings)
    for failure in report.failures:
        log.info(
            "Fast probe: instrument '%s' at %s is unreachable: %s",
            failure.instrument,
            failure.address,
            failure.reason,
        )
    return report.reachable


def run_gui(settings: Settings, simulate: bool = False) -> int:
    from gan_fet.core.command_logger import ScpiFileLogger
    from gan_fet.core.experiment import ExperimentEngine
    from gan_fet.core.safety import SafetyMonitor
    from gan_fet.instruments.rig import build_instrument_rig
    from gan_fet.sheets.sync import SheetsSync
    from gan_fet.ui.main_window import MainWindow

    if simulate:
        # Direct API callers receive the same isolation guarantees as the CLI.
        # Saving this independent object persists only simulation UI state.
        settings = settings.for_simulation()
        settings.save()

    resources = _ApplicationResources()
    try:
        resources.scpi_logger = ScpiFileLogger(
            settings.logs_dir / "gan_experiment_scpi.log"
        )
        db, recovered_runs = _open_runtime_database(settings.db_path)
        resources.db = db
        rig = build_instrument_rig(settings, simulate=simulate)
        wavegen = rig.wavegen
        scope = rig.scope
        dmm = rig.dmm
        smu = rig.smu
        wavegen_controller = rig.wavegen_controller
        resources.clients = list(rig.clients)
        safety = SafetyMonitor(
            settings.safety, smu, wavegen, db
        )
        hardware_offline = False
        if not simulate:
            if not _fast_instrument_probe(settings):
                hardware_offline = True
                log.info(
                    "Instruments unreachable during fast probe. "
                    "Opening GUI instantly in Offline Configuration Mode."
                )
            else:
                try:
                    if not safety.shutdown_outputs():
                        hardware_offline = True
                        log.warning(
                            "Instruments unreachable during startup safety check. "
                            "Opening GUI in Offline Configuration Mode."
                        )
                except Exception as exc:
                    hardware_offline = True
                    log.warning(
                        "Instruments unreachable during startup (%s). "
                        "Opening GUI in Offline Configuration Mode.", exc
                    )
        engine = ExperimentEngine(
            db,
            settings,
            smu,
            scope,
            dmm,
            wavegen,
            safety,
        )
        sheets = SheetsSync(
            settings.google, settings.default_frequencies
        )
        resources.sheets = sheets

        window = MainWindow(
            settings,
            db,
            engine,
            wavegen_controller,
            safety,
            smu,
            sheets,
            is_simulated=simulate,
            hardware_offline=hardware_offline,
            close_resources=resources.close,
        )
        sheets.status = window._status_async
        if not simulate:
            _maybe_offer_migration(db, window)
        if hardware_offline:
            window.status_bar.set_message(
                "DISCONNECTED: One or more configured instruments are unreachable. "
                "Update their connections in Configuration."
            )
        elif not sheets.available and settings.google.enabled and not simulate:
            window.status_bar.set_message(
                "Sheets sync disabled: credentials or Google libraries missing."
            )
        if simulate:
            window.status_bar.set_message(
                "SIMULATION MODE ACTIVE: using isolated virtual instruments and data."
            )
        elif recovered_runs and not hardware_offline:
            window.status_bar.set_message(
                f"Marked {recovered_runs} prior interrupted run(s) for review."
            )
        window.mainloop()
    finally:
        resources.close()
    return 0


def run_migration(settings: Settings, source: Path) -> int:
    # Live bench records take the project lease; simulation does not, so a
    # crashed practice run cannot leave a file that blocks the next start.
    db = Database(
        settings.db_path,
        use_project_lock=not settings.is_simulation_runtime,
    )
    try:
        report = migrate_legacy_tree(db, source)
        print(report.summary())
        return 0 if not report.errors else 1
    finally:
        db.close()


def run_diagnostics(settings: Settings) -> int:
    from gan_fet.core.diagnostics import run_hardware_diagnostics

    report = run_hardware_diagnostics(settings)
    print(report.summary())
    return 0 if report.all_passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gan-fet", description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--migrate",
        nargs="?",
        const=str(LEGACY_DATA_ROOT),
        metavar="PATH",
        help="import a legacy 'Device Data' CSV tree into the database and exit "
        f"(default source: {LEGACY_DATA_ROOT})",
    )
    mode.add_argument(
        "--diagnose",
        action="store_true",
        help="run hardware self-test diagnostics and exit",
    )
    mode.add_argument(
        "--simulate",
        "-s",
        action="store_true",
        help="launch with isolated virtual instruments and a simulation database",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        settings = Settings.load(
            persist_legacy_migration=not args.simulate
        )
        if not args.simulate:
            settings.save()
    except (SettingsLoadError, OSError, ValueError) as exc:
        print(f"Could not load application settings: {exc}", file=sys.stderr)
        return 2

    if args.diagnose:
        return run_diagnostics(settings)
    if args.migrate is not None:
        return run_migration(settings, Path(args.migrate))
    return run_gui(settings, simulate=args.simulate)


if __name__ == "__main__":
    raise SystemExit(main())
