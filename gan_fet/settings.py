"""Application settings: typed, JSON-persisted, no mutable module globals."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = APP_DIR / "settings.json"
LEGACY_DATA_ROOT = APP_DIR / "Device Data"

# Default parameter matrix, seeded from the original config.py
DEFAULT_CONFIGURATIONS = ["Dual Conduction", "Single Conduction", "Single Device"]
DEFAULT_FREQUENCIES = [6_000_000, 13_000_000, 27_000_000]
DEFAULT_DUTIES = [25, 50]
DEFAULT_TEMPERATURES = [25, 40, 80]
DEFAULT_VOLTAGES = [200, 300, 400]


@dataclass
class InstrumentAddress:
    ip: str
    port: int


@dataclass
class SmuSettings:
    """Keithley 2400-series SMU behind a LAN bridge (source + measure)."""

    # None → transparent bridge (plain SCPI over TCP).
    # Set to the instrument's GPIB address when using a Prologix
    # GPIB-ETHERNET bridge (which needs ++ framing commands).
    prologix_gpib_addr: Optional[int] = None
    max_voltage_v: float = 1100.0        # Keithley 2410 envelope
    current_compliance_a: float = 0.1
    nplc: float = 1.0
    sample_interval_s: float = 0.7       # matches the legacy GPIB DMM cadence
    ramp_step_v: float = 5.0             # soft-start / ramp granularity
    ramp_delay_s: float = 0.2


@dataclass
class ZvsSettings:
    """Search for the bus voltage that minimises DC input current."""

    step_v: float = 1.0
    settle_s: float = 0.5
    samples_per_point: int = 3
    min_improvement_a: float = 1e-4
    window_v: float = 50.0               # search ± this window around the start point
    max_steps: int = 200


@dataclass
class PeakControlSettings:
    """Closed-loop control of scope-measured Vds peak via the SMU setpoint."""

    tolerance_v: float = 2.0             # legacy VOLTAGE_TOLERANCE
    settle_s: float = 0.3
    max_iterations: int = 200
    min_step_v: float = 0.5
    proportional_gain: float = 0.4       # setpoint step = gain * peak error (clamped)


@dataclass
class SafetySettings:
    max_dc_current_a: float = 1.0
    max_vds_peak_v: float = 450.0
    watchdog_consecutive_failures: int = 5


@dataclass
class GoogleSettings:
    """Optional, best-effort mirror of results to Google Sheets."""

    enabled: bool = False
    credentials_file: str = "credentials.json"
    mapping_file: str = "spreadsheet_id_mappings.json"
    project_folder_id: str = "10deW0P-6I_JwlCQ66lxOE0cgC7wd_61S"
    master_spreadsheet_id: str = "1Fd3dU7KphvytGg7W_VU1HKoGyvk_ERY8OQiC0v3vwTM"


@dataclass
class Settings:
    instruments: dict[str, InstrumentAddress] = field(default_factory=lambda: {
        "SDG6022X": InstrumentAddress("10.90.6.128", 5025),
        "MSO44": InstrumentAddress("10.90.7.130", 4000),
        "SDM3055": InstrumentAddress("10.2.90.56", 5025),
        "K2400": InstrumentAddress("10.90.6.200", 1234),
    })
    smu: SmuSettings = field(default_factory=SmuSettings)
    zvs: ZvsSettings = field(default_factory=ZvsSettings)
    peak_control: PeakControlSettings = field(default_factory=PeakControlSettings)
    safety: SafetySettings = field(default_factory=SafetySettings)
    google: GoogleSettings = field(default_factory=GoogleSettings)

    data_dir: str = str(APP_DIR)
    db_filename: str = "gan_fet.db"

    default_configurations: list[str] = field(default_factory=lambda: list(DEFAULT_CONFIGURATIONS))
    default_frequencies: list[int] = field(default_factory=lambda: list(DEFAULT_FREQUENCIES))
    default_duties: list[int] = field(default_factory=lambda: list(DEFAULT_DUTIES))
    default_temperatures: list[int] = field(default_factory=lambda: list(DEFAULT_TEMPERATURES))
    default_voltages: list[int] = field(default_factory=lambda: list(DEFAULT_VOLTAGES))
    voltage_default_minutes: dict[str, float] = field(
        default_factory=lambda: {"200": 1, "300": 1, "400": 1}
    )

    # UI state persisted between sessions (replaces last_params.json)
    last_params: dict[str, Any] = field(default_factory=dict)
    find_zvs_before_run: bool = False

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / self.db_filename

    @property
    def screenshots_dir(self) -> Path:
        return Path(self.data_dir) / "screenshots"

    def default_duration_minutes(self, voltage: int) -> float:
        return float(self.voltage_default_minutes.get(str(voltage), 1))

    # -- persistence -------------------------------------------------

    def save(self, path: Path = DEFAULT_SETTINGS_PATH) -> None:
        path.write_text(json.dumps(dataclasses.asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path = DEFAULT_SETTINGS_PATH) -> "Settings":
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return cls()
        return cls._from_dict(raw)

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> "Settings":
        settings = cls()
        for f in dataclasses.fields(cls):
            if f.name not in raw:
                continue
            value = raw[f.name]
            try:
                if f.name == "instruments":
                    settings.instruments = {
                        name: InstrumentAddress(str(v["ip"]), int(v["port"]))
                        for name, v in value.items()
                    }
                elif f.name == "smu":
                    settings.smu = SmuSettings(**value)
                elif f.name == "zvs":
                    settings.zvs = ZvsSettings(**value)
                elif f.name == "peak_control":
                    settings.peak_control = PeakControlSettings(**value)
                elif f.name == "safety":
                    settings.safety = SafetySettings(**value)
                elif f.name == "google":
                    settings.google = GoogleSettings(**value)
                else:
                    setattr(settings, f.name, value)
            except (TypeError, KeyError, ValueError):
                pass  # keep the default for malformed sections
        return settings
