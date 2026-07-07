"""CSV export of runs and samples for sharing / external analysis."""

from __future__ import annotations

import csv
from pathlib import Path

from gan_fet.storage.db import Database

RUN_HEADER = [
    "Device", "Configuration", "Frequency (Hz)", "Duty (%)", "Temperature (°C)",
    "Voltage (V)", "Status", "Started", "Completed", "Bus Voltage (V)",
    "V_ZVS (V)", "Vin (V)", "Iin (A)", "fsw (Hz)", "Irms (A)",
    "Vds peak (V)", "Isw RMS (A)", "Screenshot",
]

SAMPLE_HEADER = [
    "Run ID", "Device", "Configuration", "Frequency (Hz)", "Duty (%)",
    "Temperature (°C)", "Voltage (V)", "Timestamp", "DC Current (A)",
    "SMU Voltage (V)", "DC Voltage (V)", "RMS Current (A)", "Isw RMS (A)",
]


def export_device(db: Database, device_name: str, dest_dir: Path) -> list[Path]:
    """Write <device>_runs.csv and <device>_samples.csv; returns the paths."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    runs = db.runs_for_device(device_name)

    runs_path = dest_dir / f"{device_name}_runs.csv"
    with runs_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(RUN_HEADER)
        for run in runs:
            p, r = run.point, run.readings
            writer.writerow([
                p.device_name, p.config, p.frequency_hz, p.duty_pct,
                p.temperature_c, p.voltage_v, run.status, run.started_at,
                run.completed_at, run.bus_voltage_v, run.v_zvs, r.vin, r.iin,
                r.fsw_hz, r.irms, r.vds_pk, r.isw_rms, run.screenshot_path,
            ])

    samples_path = dest_dir / f"{device_name}_samples.csv"
    with samples_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(SAMPLE_HEADER)
        for run in runs:
            p = run.point
            for ts, dc_i, smu_v, dc_v, rms, isw in db.samples_for_run(run.id):
                writer.writerow([
                    run.id, p.device_name, p.config, p.frequency_hz, p.duty_pct,
                    p.temperature_c, p.voltage_v, ts, dc_i, smu_v, dc_v, rms, isw,
                ])

    return [runs_path, samples_path]
