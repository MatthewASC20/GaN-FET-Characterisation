"""One-time import of the legacy `Device Data/` CSV tree into SQLite.

Legacy layout (produced by the v1 app):
    Device Data/<device>/<N>MHz/<duty>/<device>_<config>_<T>C_<N>MHz_<V>V_<duty>duty.csv
Sample rows:  ts, device, temp, freq, volt, config, duty, current
Final row:    FINAL_READINGS, vin, irms, iin, fsw [, isw_rms, vds_pk]
A sibling .png (same stem) is the oscilloscope screenshot for the run.

Idempotent: matrix points that already have a run in the database are skipped.
"""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from gan_fet.core.models import FinalReadings, MatrixPoint
from gan_fet.storage.db import Database


@dataclass
class MigrationReport:
    runs_imported: int = 0
    runs_partial: int = 0
    runs_skipped_existing: int = 0
    samples_imported: int = 0
    devices: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"Imported {self.runs_imported} runs "
            f"({self.samples_imported} samples) from "
            f"{len(self.devices)} devices.",
        ]
        if self.runs_skipped_existing:
            lines.append(f"Skipped {self.runs_skipped_existing} runs already in the database.")
        if self.runs_partial:
            lines.append(
                f"Preserved {self.runs_partial} legacy runs with incomplete final readings "
                "as 'legacy_partial'."
            )
        if self.errors:
            lines.append(f"{len(self.errors)} files could not be parsed:")
            lines.extend(f"  - {e}" for e in self.errors[:10])
            if len(self.errors) > 10:
                lines.append(f"  ... and {len(self.errors) - 10} more")
        return "\n".join(lines)


def _parse_filename(device: str, filename: str) -> Optional[MatrixPoint]:
    pattern = (
        rf"^{re.escape(device)}_(?P<config>.+)_(?P<temp>\d+)C_"
        rf"(?P<freq>\d+)MHz_(?P<volt>\d+)V_(?P<duty>\d+)duty\.csv$"
    )
    m = re.match(pattern, filename)
    if not m:
        return None
    return MatrixPoint(
        device_name=device,
        config=m.group("config"),
        frequency_hz=int(m.group("freq")) * 1_000_000,
        duty_pct=int(m.group("duty")),
        temperature_c=int(m.group("temp")),
        voltage_v=int(m.group("volt")),
    )


def _float_or_none(raw: str) -> Optional[float]:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_csv(path: Path) -> tuple[list[tuple], FinalReadings, bool]:
    """Return sample rows, final readings and whether a final row was present."""
    samples: list[tuple] = []
    readings = FinalReadings()
    final_seen = False
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.reader(fh):
            if not row or not row[0].strip():
                continue
            first = row[0].strip()
            if first == "Timestamp":
                continue  # header
            if first == "FINAL_READINGS":
                final_seen = True
                # FINAL_READINGS, vin, irms, iin, fsw [, isw_rms, vds_pk]
                readings.vin = _float_or_none(row[1]) if len(row) > 1 else None
                readings.irms = _float_or_none(row[2]) if len(row) > 2 else None
                readings.iin = _float_or_none(row[3]) if len(row) > 3 else None
                readings.fsw_hz = _float_or_none(row[4]) if len(row) > 4 else None
                readings.isw_rms = _float_or_none(row[5]) if len(row) > 5 else None
                readings.vds_pk = _float_or_none(row[6]) if len(row) > 6 else None
                continue
            if len(row) >= 8:
                # ts, device, temp, freq, volt, config, duty, current
                current = _float_or_none(row[7])
                samples.append((first, current, None, None, None, None))
    return samples, readings, final_seen


def _has_required_final_readings(
    readings: FinalReadings,
    config: str,
) -> bool:
    """Apply the same completeness contract as a live experiment."""
    required = [
        readings.vin,
        readings.iin,
        readings.fsw_hz,
        readings.irms,
        readings.vds_pk,
    ]
    if config == "Dual Conduction":
        required.append(readings.isw_rms)
    return all(value is not None and math.isfinite(value) for value in required)


def _import_device_options(db: Database, device_dir: Path) -> None:
    config_path = device_dir / "device_config.json"
    if not config_path.is_file():
        return
    try:
        options = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if isinstance(options, dict) and db.get_device_options(device_dir.name) is None:
        db.set_device_options(device_dir.name, options)


def migrate_legacy_tree(db: Database, root: Path) -> MigrationReport:
    report = MigrationReport()
    if not root.is_dir():
        return report

    for device_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        device = device_dir.name
        _import_device_options(db, device_dir)
        for csv_path in sorted(device_dir.rglob("*.csv")):
            point = _parse_filename(device, csv_path.name)
            if point is None:
                report.errors.append(str(csv_path.relative_to(root)))
                continue

            try:
                samples, readings, final_seen = _parse_csv(csv_path)
            except Exception as exc:
                report.errors.append(f"{csv_path.relative_to(root)}: {exc}")
                continue

            started_at = samples[0][0] if samples else None
            completed_at = samples[-1][0] if samples else None
            is_complete = final_seen and _has_required_final_readings(
                readings, point.config
            )
            screenshot = csv_path.with_suffix(".png")
            try:
                with db.transaction():
                    # Keep the check and insert in the same write transaction:
                    # append-only attempts otherwise make a check/insert race possible.
                    existing_attempts = db.run_attempts_for_point(point)
                    has_completed = any(
                        attempt.status == "completed"
                        for attempt in existing_attempts
                    )
                    has_same_partial = not is_complete and any(
                        attempt.status == "legacy_partial"
                        for attempt in existing_attempts
                    )
                    if has_completed or has_same_partial:
                        report.runs_skipped_existing += 1
                        continue

                    run_id = db.create_run(
                        point,
                        duration_minutes=0.0,
                        status="importing" if is_complete else "legacy_partial",
                        started_at=started_at,
                        replace_existing=False,
                    )
                    if samples:
                        db.add_samples(run_id, samples)

                    screenshot_path = (
                        str(screenshot.resolve()) if screenshot.is_file() else None
                    )
                    if is_complete:
                        db.complete_run(
                            run_id,
                            readings,
                            screenshot_path=screenshot_path,
                            completed_at=completed_at,
                        )
                    elif screenshot_path is not None:
                        db.set_run_screenshot(run_id, screenshot_path)
            except Exception as exc:
                report.errors.append(f"{csv_path.relative_to(root)}: {exc}")
                continue

            report.runs_imported += 1
            if not is_complete:
                report.runs_partial += 1
            report.samples_imported += len(samples)
            report.devices.add(device)

    return report
