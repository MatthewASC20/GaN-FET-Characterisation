"""Composition root and CLI entry point.

    gan-fet             launch the GUI
    gan-fet --migrate   import the legacy `Device Data/` CSV tree, then exit
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from gan_fet.settings import LEGACY_DATA_ROOT, Settings
from gan_fet.storage.db import Database
from gan_fet.storage.migrate import migrate_legacy_tree

log = logging.getLogger(__name__)


def _build_instruments(settings: Settings):
    from gan_fet.core.autotune import WavegenController
    from gan_fet.instruments.multimeter import Sdm3055
    from gan_fet.instruments.oscilloscope import Mso44
    from gan_fet.instruments.scpi import ScpiTcpClient
    from gan_fet.instruments.smu import Keithley2400
    from gan_fet.instruments.wavegen import Sdg6022x

    def client(name: str) -> ScpiTcpClient:
        addr = settings.instruments[name]
        return ScpiTcpClient(name, addr.ip, addr.port)

    wavegen = Sdg6022x(client("SDG6022X"))
    scope = Mso44(client("MSO44"))
    dmm = Sdm3055(client("SDM3055"))
    smu = Keithley2400(client("K2400"), settings.smu)
    return wavegen, scope, dmm, smu, WavegenController(wavegen)


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


def run_gui(settings: Settings) -> int:
    from gan_fet.core.experiment import ExperimentEngine
    from gan_fet.core.safety import SafetyMonitor
    from gan_fet.sheets.sync import SheetsSync
    from gan_fet.ui.main_window import MainWindow

    db = Database(settings.db_path)
    wavegen, scope, dmm, smu, wavegen_controller = _build_instruments(settings)
    safety = SafetyMonitor(settings.safety, smu, wavegen, db)
    engine = ExperimentEngine(db, settings, smu, scope, dmm, wavegen, safety)
    sheets = SheetsSync(settings.google, settings.default_frequencies)

    window = MainWindow(settings, db, engine, wavegen_controller, safety, smu, sheets)
    sheets.status = window._status_async
    _maybe_offer_migration(db, window)
    if not sheets.available and settings.google.enabled:
        window.status_bar.set_message(
            "Sheets sync disabled: credentials.json or google libraries missing."
        )
    try:
        window.mainloop()
    finally:
        db.close()
    return 0


def run_migration(settings: Settings, source: Path) -> int:
    db = Database(settings.db_path)
    try:
        report = migrate_legacy_tree(db, source)
        print(report.summary())
        return 0 if not report.errors else 1
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gan-fet", description=__doc__)
    parser.add_argument(
        "--migrate",
        nargs="?",
        const=str(LEGACY_DATA_ROOT),
        metavar="PATH",
        help="import a legacy 'Device Data' CSV tree into the database and exit "
        f"(default source: {LEGACY_DATA_ROOT})",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = Settings.load()
    settings.save()  # materialise defaults on first run

    if args.migrate is not None:
        return run_migration(settings, Path(args.migrate))
    return run_gui(settings)


if __name__ == "__main__":
    sys.exit(main())
