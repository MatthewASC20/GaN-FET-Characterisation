"""Shared dataclasses and enums (no UI, no I/O)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ExperimentState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class MatrixPoint:
    """One cell of the test matrix — the natural key of a run."""

    device_name: str
    config: str
    frequency_hz: int
    duty_pct: int
    temperature_c: int
    voltage_v: int

    def describe(self) -> str:
        return (
            f"{self.device_name}: {self.config}, {self.frequency_hz / 1e6:.0f} MHz, "
            f"{self.duty_pct}% duty, {self.voltage_v} V, {self.temperature_c} °C"
        )


@dataclass
class ExperimentParams:
    point: MatrixPoint
    duration_minutes: float
    find_zvs: bool = False


@dataclass
class FinalReadings:
    vin: Optional[float] = None          # DC bus voltage (multimeter)
    iin: Optional[float] = None          # DC input current (SMU)
    fsw_hz: Optional[float] = None       # measured wavegen frequency
    irms: Optional[float] = None         # scope RMS current (run average)
    vds_pk: Optional[float] = None       # scope peak Vds
    isw_rms: Optional[float] = None      # scope switch-current RMS


@dataclass
class RunRecord:
    id: int
    point: MatrixPoint
    duration_minutes: Optional[float]
    started_at: Optional[str]
    completed_at: Optional[str]
    status: str
    bus_voltage_v: Optional[float] = None
    v_zvs: Optional[float] = None
    readings: FinalReadings = field(default_factory=FinalReadings)
    screenshot_path: Optional[str] = None


def freq_label(freq_hz: float) -> str:
    """Human label used throughout the app and the legacy file tree, e.g. 13MHz."""
    return f"{int(freq_hz / 1e6)}MHz"


def sanitize_device_name(raw_name: str) -> str:
    """Sanitise device names for filesystem/DB usage (legacy-compatible)."""
    if not raw_name:
        return ""
    return "".join(
        c for c in raw_name.strip() if c.isalnum() or c in (" ", "_", "-")
    ).rstrip()
