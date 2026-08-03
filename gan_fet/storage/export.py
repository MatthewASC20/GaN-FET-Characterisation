"""CSV export of runs and samples for sharing / external analysis."""

from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path

from gan_fet.storage.db import Database
from gan_fet.storage.paths import device_output_path

RUN_HEADER = [
    "Run ID", "Device", "Configuration", "Frequency (Hz)", "Duty (%)",
    "Temperature (°C)", "Voltage (V)", "Attempt", "Status", "Started",
    "Completed", "Bus Voltage (V)", "V_ZVS (V)", "Vin (V)", "Iin (A)",
    "fsw (Hz)", "Irms (A)", "Vds peak (V)", "Isw RMS (A)", "Screenshot",
]

SAMPLE_HEADER = [
    "Run ID", "Device", "Configuration", "Frequency (Hz)", "Duty (%)",
    "Temperature (°C)", "Voltage (V)", "Timestamp", "Elapsed (s)",
    "DC Current (A)", "SMU Voltage (V)", "DC Voltage (V)", "RMS Current (A)",
    "Isw RMS (A)",
]


def export_device(
    db: Database,
    device_name: str,
    dest_dir: Path,
    *,
    is_simulated: bool = False,
) -> list[Path]:
    """Export every run attempt and its samples as one consistent snapshot."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    with db.transaction():
        runs = db.all_run_attempts_for_device(device_name)
        samples_by_run = {
            run.id: db.samples_for_run(run.id)
            for run in runs
        }

    filename_prefix = "SIMULATION_" if is_simulated else ""
    runs_path = device_output_path(
        dest_dir, device_name, f"{filename_prefix}runs.csv"
    )
    samples_path = device_output_path(
        dest_dir, device_name, f"{filename_prefix}samples.csv"
    )
    origin = ["SIMULATION — NOT MEASURED DATA"] if is_simulated else []
    temp_paths: list[Path] = []
    try:
        runs_temp = _temporary_output(dest_dir)
        temp_paths.append(runs_temp)
        with runs_temp.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow((["Data Origin"] if is_simulated else []) + RUN_HEADER)
            for run in runs:
                p, r = run.point, run.readings
                writer.writerow(origin + [
                    run.id, p.device_name, p.config, p.frequency_hz, p.duty_pct,
                    p.temperature_c, p.voltage_v, run.attempt_no, run.status, run.started_at,
                    run.completed_at, run.bus_voltage_v, run.tuned_voltage_v, r.vin, r.iin,
                    r.fsw_hz, r.irms, r.vds_pk, r.isw_rms, run.screenshot_path,
                ])

        samples_temp = _temporary_output(dest_dir)
        temp_paths.append(samples_temp)
        with samples_temp.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                (["Data Origin"] if is_simulated else []) + SAMPLE_HEADER
            )
            for run in runs:
                p = run.point
                for ts, dc_i, smu_v, dc_v, rms, isw, elapsed_s in samples_by_run[
                    run.id
                ]:
                    writer.writerow(origin + [
                        run.id, p.device_name, p.config, p.frequency_hz, p.duty_pct,
                        p.temperature_c, p.voltage_v, ts, elapsed_s, dc_i, smu_v,
                        dc_v, rms, isw,
                    ])

        os.replace(runs_temp, runs_path)
        temp_paths.remove(runs_temp)
        os.replace(samples_temp, samples_path)
        temp_paths.remove(samples_temp)
    finally:
        for temp_path in temp_paths:
            temp_path.unlink(missing_ok=True)

    return [runs_path, samples_path]


def _temporary_output(dest_dir: Path) -> Path:
    handle = tempfile.NamedTemporaryFile(
        prefix=".gan-fet-export-", suffix=".tmp", dir=dest_dir, delete=False
    )
    handle.close()
    return Path(handle.name)
