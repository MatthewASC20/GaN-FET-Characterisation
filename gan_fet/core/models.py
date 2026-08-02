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
            f"{self.device_name}: {self.config}, {freq_label(self.frequency_hz)}, "
            f"{self.duty_pct}% duty, {self.voltage_v} V, {self.temperature_c} °C"
        )


@dataclass
class ExperimentParams:
    point: MatrixPoint
    duration_minutes: float
    find_zvs: bool = False
    #: Search the gate frequency for minimum input power before sampling.
    find_frequency: bool = False


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
    attempt_no: int = 1
    bus_voltage_v: Optional[float] = None
    v_zvs: Optional[float] = None
    readings: FinalReadings = field(default_factory=FinalReadings)
    screenshot_path: Optional[str] = None
    #: Gate frequency chosen by the frequency search, and the input power it
    #: achieved. ``None`` when the run did not tune.
    tuned_frequency_hz: Optional[float] = None
    tuned_input_power_w: Optional[float] = None
    #: Direction the frequency window was swept. Recorded because the plant is
    #: not strictly single-valued; without it, path dependence is undetectable
    #: after the fact.
    sweep_direction: Optional[str] = None
    #: Fraction of the cycle Vds spent below the ZVS threshold. Zero means
    #: hard switching; ``None`` means the measurement was unavailable, which
    #: is a different conclusion entirely.
    zvs_dwell_fraction: Optional[float] = None


@dataclass
class TripContext:
    """Operating point captured at the moment a safety event fires.

    Trips during manual bench work carry no run association, which historically
    left them undiagnosable. Every field is optional: telemetry is frequently
    the thing that has just failed.
    """

    frequency_hz: Optional[float] = None
    bus_setpoint_v: Optional[float] = None
    vds_peak_v: Optional[float] = None
    dc_current_a: Optional[float] = None


def freq_label(freq_hz: float) -> str:
    """Return an exact, compact MHz label without truncating fractional MHz."""
    frequency = float(freq_hz)
    decimal_places = 6 if frequency.is_integer() else 9
    mhz = f"{frequency / 1_000_000:.{decimal_places}f}".rstrip("0").rstrip(".")
    return f"{mhz}MHz"


def sanitize_device_name(raw_name: str) -> str:
    """Sanitise device names for filesystem/DB usage (legacy-compatible)."""
    if not raw_name:
        return ""
    return "".join(
        c for c in raw_name.strip() if c.isalnum() or c in (" ", "_", "-")
    ).rstrip()
