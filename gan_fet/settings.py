"""Typed application settings with safe, per-user JSON persistence."""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, TypeVar, cast

log = logging.getLogger(__name__)

APP_NAME = "GaN-FET-Characterisation"

# ``APP_DIR`` remains the legacy checkout/install root.  Runtime files no
# longer default here, but it is still where older releases stored their data.
APP_DIR = Path(__file__).resolve().parent.parent
LEGACY_DATA_ROOT = APP_DIR / "Device Data"


def _user_config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home())
        return base / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME


def _user_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home())
        return base / APP_NAME / "data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME / "data"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME


def _user_log_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home())
        return base / APP_NAME / "logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / APP_NAME
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / APP_NAME / "logs"


DEFAULT_CONFIG_DIR = _user_config_dir()
DEFAULT_DATA_DIR = _user_data_dir()
DEFAULT_LOG_DIR = _user_log_dir()
DEFAULT_SETTINGS_PATH = DEFAULT_CONFIG_DIR / "settings.json"

SIMULATION_DIRNAME = "simulation"
SIMULATION_DB_FILENAME = "gan_fet_simulation.db"
SIMULATION_SETTINGS_FILENAME = "settings.json"
SIMULATION_DEFAULT_DEVICE_NAME = "SIMULATED-DUT"

# Search only when the new per-user settings file does not exist.  This keeps
# an existing checkout/ZIP installation working while moving future writes to
# a writable user location.
LEGACY_SETTINGS_PATHS = (
    APP_DIR / "settings.json",
    Path.cwd() / "settings.json",
)

# Default parameter matrix, seeded from the original config.py
DEFAULT_CONFIGURATIONS = ["Dual Conduction", "Single Conduction", "Single Device"]
DEFAULT_FREQUENCIES = [6_000_000, 13_000_000, 27_000_000]
DEFAULT_DUTIES = [25, 50]
DEFAULT_TEMPERATURES = [25, 40, 80]
DEFAULT_VOLTAGES = [200, 300, 400]

SCOPE_INSTRUMENT_KEY = "HDO4054"
SCOPE_MODEL = SCOPE_INSTRUMENT_KEY
_LEGACY_LECROY_SCOPE_KEY = "LeCroy"


class SettingsLoadError(RuntimeError):
    """Raised when an existing settings file cannot be safely loaded."""


@dataclass
class InstrumentAddress:
    """Persisted connection target.

    ``ip`` and ``port`` retain their historical JSON names. ``ip`` may now be
    a hostname, VISA resource, serial device, or explicit transport URI; for
    non-TCP targets ``port`` is the legacy baud/placeholder value.
    """

    ip: str
    port: int

    @property
    def target(self) -> str:
        """Transport-neutral name for new code."""
        return self.ip


_MISSING = object()
_HDO_VXI11_RESOURCE = re.compile(
    r"TCPIP\d*::[^:]+(?:::inst\d+)?::INSTR",
    re.IGNORECASE,
)


def _validate_hdo_transport(target: str, port: int, *, key: str) -> None:
    """Require a message-framed VISA session for the HDO4054."""
    requirement = (
        f"{key} endpoint must use an LXI/VXI-11 VISA resource with port 0"
    )
    if (
        not isinstance(target, str)
        or isinstance(port, bool)
        or not isinstance(port, int)
        or port != 0
        or _HDO_VXI11_RESOURCE.fullmatch(target) is None
    ):
        raise ValueError(requirement)

    from gan_fet.transport.address import AddressError, parse_address

    try:
        spec = parse_address(target, port)
    except AddressError as exc:
        raise ValueError(f"{key} endpoint is invalid: {exc}") from exc
    if not spec.is_visa:
        raise ValueError(requirement)


def _scope_endpoint(
    instruments: dict[str, Any], key: str
) -> Optional[dict[str, Any]]:
    """Return one normalized scope endpoint, rejecting ambiguous bad data."""
    value = instruments.get(key, _MISSING)
    if value is _MISSING:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{key} endpoint must be a JSON object")

    target = value.get("ip")
    port = value.get("port")
    if not isinstance(target, str) or not target.strip():
        raise ValueError(f"{key} endpoint must contain a non-empty target")
    if port is None or isinstance(port, bool):
        raise ValueError(f"{key} endpoint port must be an integer")
    try:
        normalized_port = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} endpoint port must be an integer") from exc

    normalized_target = target.strip()
    _validate_hdo_transport(normalized_target, normalized_port, key=key)
    return {"ip": normalized_target, "port": normalized_port}


def _normalize_scope_payload(
    raw: dict[str, Any], *, require_scope_evidence: bool
) -> dict[str, Any]:
    """Canonicalize the single supported scope before defaults are merged.

    Existing files are validated in strict mode.  Only the generic legacy
    LeCroy key is known to use the same MAUI command dialect and can therefore
    be migrated automatically.  Every other legacy selection remains
    untouched on disk and requires an operator to confirm the physical scope
    replacement before this release will run.
    """
    normalized = dict(raw)
    compatible_model = False
    if "scope_model" in normalized:
        model = normalized["scope_model"]
        if not isinstance(model, str) or model.strip().casefold() not in {
            SCOPE_MODEL.casefold(),
            _LEGACY_LECROY_SCOPE_KEY.casefold(),
        }:
            raise ValueError(
                "scope_model must identify the Teledyne LeCroy HDO4054"
            )
        compatible_model = True

    raw_instruments = normalized.get("instruments", _MISSING)
    selected_endpoint: Optional[dict[str, Any]] = None
    if raw_instruments is not _MISSING:
        if not isinstance(raw_instruments, dict):
            raise ValueError("instruments must be a JSON object")

        hdo_endpoint = _scope_endpoint(raw_instruments, SCOPE_INSTRUMENT_KEY)
        legacy_endpoint = _scope_endpoint(
            raw_instruments, _LEGACY_LECROY_SCOPE_KEY
        )
        if (
            hdo_endpoint is not None
            and legacy_endpoint is not None
            and hdo_endpoint != legacy_endpoint
        ):
            raise ValueError(
                "the HDO4054 and legacy LeCroy endpoints conflict"
            )
        selected_endpoint = hdo_endpoint or legacy_endpoint
        if selected_endpoint is None and require_scope_evidence:
            raise ValueError(
                "the instrument map has no HDO4054-compatible "
                "oscilloscope endpoint"
            )
        if selected_endpoint is not None:
            migrated_instruments = dict(raw_instruments)
            migrated_instruments.pop(_LEGACY_LECROY_SCOPE_KEY, None)
            migrated_instruments[SCOPE_INSTRUMENT_KEY] = selected_endpoint
            normalized["instruments"] = migrated_instruments
            compatible_model = True

    if require_scope_evidence and not compatible_model:
        raise ValueError(
            "the settings contain no confirmed HDO4054 configuration"
        )

    normalized["scope_model"] = SCOPE_MODEL
    return normalized


@dataclass
class SmuSettings:
    """Keithley 2400-series SMU source/measure configuration.

    The GPIB address is needed only for a Prologix serial/Ethernet bridge.
    Direct VISA resources already contain their GPIB address.
    """

    prologix_gpib_addr: Optional[int] = None
    max_voltage_v: float = 1100.0
    current_compliance_a: float = 0.1
    nplc: float = 1.0
    sample_interval_s: float = 0.7
    ramp_step_v: float = 5.0
    ramp_rate_v_s: float = 25.0

    def ramp_delay_for_step(self, step_v: Optional[float] = None) -> float:
        """Translate the canonical voltage rate into a per-step delay."""
        step = abs(float(self.ramp_step_v if step_v is None else step_v))
        rate = float(self.ramp_rate_v_s)
        if (
            not math.isfinite(step)
            or step <= 0
            or not math.isfinite(rate)
            or rate <= 0
        ):
            raise ValueError("SMU ramp step and rate must be finite and positive")
        return step / rate

    @property
    def ramp_delay_s(self) -> float:
        """Computed compatibility view for callers from pre-rate releases.

        Delay is no longer persisted independently because it describes the
        same behavior as ``ramp_rate_v_s`` and could otherwise contradict it.
        """
        return self.ramp_delay_for_step()

    @ramp_delay_s.setter
    def ramp_delay_s(self, value: float) -> None:
        delay = float(value)
        step = abs(float(self.ramp_step_v))
        if (
            not math.isfinite(delay)
            or delay <= 0
            or not math.isfinite(step)
            or step <= 0
        ):
            raise ValueError("SMU ramp step and delay must be finite and positive")
        self.ramp_rate_v_s = step / delay


@dataclass
class WavegenSettings:
    """Waveform generator frequency and duty-cycle ramping parameters."""

    freq_ramp_rate_khz_s: float = 200.0
    duty_ramp_rate_pct_s: float = 10.0
    duty_step_pct: float = 1.0


@dataclass
class ZvsSettings:
    """Search for the bus voltage that minimises DC input current."""

    step_v: float = 1.0
    settle_s: float = 0.5
    samples_per_point: int = 3
    min_improvement_a: float = 1e-4
    window_v: float = 50.0
    max_steps: int = 200
    autotune_freq_rate_khz_s: float = 200.0


@dataclass
class PeakControlSettings:
    """Closed-loop control of scope-measured Vds peak via the SMU setpoint."""

    tolerance_v: float = 2.0
    settle_s: float = 0.3
    max_iterations: int = 200
    min_step_v: float = 0.5
    proportional_gain: float = 0.4
    #: Minimum bus separation before a secant gain estimate is trusted. Below
    #: this the finite difference is mostly measurement noise.
    secant_min_delta_v: float = 0.4
    #: Largest single bus correction the secant loop may command.
    secant_max_step_v: float = 10.0


@dataclass
class FrequencyTuneSettings:
    """Search for the gate frequency that minimises input power.

    The objective is ``P_in = V_bus x I_dc`` at a held Vds peak, not DC input
    current: because the bus voltage varies along the constant-peak curve,
    the two have different minima.
    """

    #: Working window as a fraction of nominal when no warm start exists.
    #: Across 460 historical runs the tuned frequency departed from nominal by
    #: up to 21.7% (median 8.5%, p90 15.6%), so 0.20 would have clipped the
    #: worst cases outright.
    window_frac: float = 0.25
    #: Wide low-amplitude survey window, used when the bank is unknown.
    survey_window_frac: float = 0.50
    survey_step_hz: float = 200_000.0
    #: Survey runs at this fraction of target peak, where Coss varies little.
    survey_peak_frac: float = 0.18
    #: The small-signal resonance sits below the working one; bias the window
    #: up. Measured drift is +6.3% median from 200 V to 400 V, and the survey
    #: sits far below 200 V, so this is an extrapolation beyond the data.
    #: Precision is not critical: the window width dominates, and this only has
    #: to put the true optimum comfortably inside it.
    survey_upward_bias_frac: float = 0.08
    coarse_step_hz: float = 100_000.0
    fine_step_hz: float = 20_000.0
    fine_span_hz: float = 100_000.0
    #: Warm-started window half-width, as a fraction of the warm-start
    #: frequency. Fractional rather than absolute because drift scales with the
    #: band: a fixed 300 kHz is 5% at 6 MHz but only 1.1% at 27 MHz.
    warm_window_frac: float = 0.05
    settle_s: float = 0.5
    #: Peak excursion tolerated while stepping frequency, before the bus is
    #: backed off. Distinct from the hard safety ceiling, which trips.
    soft_band_v: float = 10.0
    #: Fraction of the hard ceiling used when computing the reachability cap.
    ceiling_margin_frac: float = 0.955
    #: Headroom above the largest observed tank gain. A 400 V target under a
    #: 450 V ceiling leaves only 12% room, so this must stay near unity.
    gain_growth_factor: float = 1.05
    #: A step change exceeding this multiple of the locally predicted change is
    #: recorded as an anomaly rather than as smooth data.
    anomaly_ratio: float = 4.0
    #: ZVS dwell threshold as a fraction of target Vds peak. 2.5% is the
    #: scaling arrived at empirically on the bench: 5 V at 200 V, 10 V at
    #: 400 V. Must stay above the ringing on the zero dwell or the
    #: measurement chatters.
    zvs_threshold_frac: float = 0.025
    max_points: int = 400


@dataclass
class SafetySettings:
    max_dc_current_a: float = 1.0
    max_vds_peak_v: float = 450.0
    watchdog_consecutive_failures: int = 5


@dataclass
class GoogleSettings:
    """Optional, best-effort mirror of results to Google Sheets."""

    enabled: bool = False
    credentials_file: str = str(DEFAULT_CONFIG_DIR / "credentials.json")
    mapping_file: str = str(DEFAULT_CONFIG_DIR / "spreadsheet_id_mappings.json")
    project_folder_id: str = "10deW0P-6I_JwlCQ66lxOE0cgC7wd_61S"
    master_spreadsheet_id: str = "1Fd3dU7KphvytGg7W_VU1HKoGyvk_ERY8OQiC0v3vwTM"


_T = TypeVar("_T")


def _dataclass_from_mapping(cls: type[_T], value: Any, fallback: _T) -> _T:
    """Load known fields only, retaining defaults for malformed/newer values."""
    if not isinstance(value, dict):
        return fallback
    known = {
        f.name for f in dataclasses.fields(cast(Any, cls)) if f.init
    }
    merged = dataclasses.asdict(cast(Any, fallback))
    merged.update({key: val for key, val in value.items() if key in known})
    try:
        return cls(**merged)
    except (TypeError, ValueError):
        return fallback


def _reject_json_constant(value: str) -> None:
    """Reject JavaScript-only numeric constants accepted by ``json`` by default."""
    raise ValueError(f"non-finite JSON number {value!r} is not permitted")


def _positive_number(value: Any, fallback: float, *, integer: bool = False):
    """Return a finite positive number of the requested kind, or its default."""
    if isinstance(value, bool):
        return fallback
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number) or number <= 0:
        return fallback
    if integer:
        if not number.is_integer():
            return fallback
        return int(number)
    return number


def _normalize_smu_mapping(value: Any) -> Any:
    """Migrate the retired fixed-delay control to the canonical ramp rate.

    Older files may contain only ``ramp_delay_s``.  Transitional releases
    persisted both controls even though the rate silently won at runtime.  A
    customized legacy delay is therefore preserved when the stored rate is
    absent, invalid, or still at its default; an explicitly customized rate
    takes precedence when both were changed.
    """
    if not isinstance(value, dict) or "ramp_delay_s" not in value:
        return value

    normalized = dict(value)
    delay_raw = normalized.pop("ramp_delay_s")
    defaults = SmuSettings()

    def positive_float(raw: Any) -> Optional[float]:
        if isinstance(raw, bool):
            return None
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number > 0 else None

    delay = positive_float(delay_raw)
    if delay is None:
        log.warning(
            "Invalid legacy setting smu.ramp_delay_s=%r; ignoring it",
            delay_raw,
        )
        return normalized

    step = positive_float(normalized.get("ramp_step_v")) or defaults.ramp_step_v
    derived_rate = step / delay
    rate_present = "ramp_rate_v_s" in normalized
    rate = positive_float(normalized.get("ramp_rate_v_s"))
    delay_is_custom = not math.isclose(
        delay,
        defaults.ramp_delay_s,
        rel_tol=1e-9,
        abs_tol=1e-12,
    )
    rate_is_default = rate is not None and math.isclose(
        rate,
        defaults.ramp_rate_v_s,
        rel_tol=1e-9,
        abs_tol=1e-12,
    )

    if (
        not rate_present
        or rate is None
        or (rate_is_default and delay_is_custom)
    ):
        normalized["ramp_rate_v_s"] = derived_rate
        log.info(
            "Migrated legacy SMU ramp delay %.6g s at %.6g V/step "
            "to %.6g V/s",
            delay,
            step,
            derived_rate,
        )
    elif not math.isclose(
        rate,
        derived_rate,
        rel_tol=1e-9,
        abs_tol=1e-12,
    ):
        log.warning(
            "Both smu.ramp_rate_v_s and legacy smu.ramp_delay_s were "
            "customized; keeping the explicit ramp rate %.6g V/s",
            rate,
        )
    return normalized


@dataclass
class Settings:
    instruments: dict[str, InstrumentAddress] = field(
        default_factory=lambda: {
            "SDG6022X": InstrumentAddress("10.11.13.230", 5025),
            SCOPE_INSTRUMENT_KEY: InstrumentAddress(
                "TCPIP0::10.11.13.231::inst0::INSTR", 0
            ),
            "K2410": InstrumentAddress("GPIB0::24::INSTR", 0),
        }
    )
    smu: SmuSettings = field(default_factory=SmuSettings)
    wavegen: WavegenSettings = field(default_factory=WavegenSettings)
    zvs: ZvsSettings = field(default_factory=ZvsSettings)
    frequency_tune: FrequencyTuneSettings = field(
        default_factory=FrequencyTuneSettings
    )
    peak_control: PeakControlSettings = field(default_factory=PeakControlSettings)
    safety: SafetySettings = field(default_factory=SafetySettings)
    google: GoogleSettings = field(default_factory=GoogleSettings)
    # Persisted as a fail-closed compatibility discriminator.  This release
    # intentionally has no runtime scope-model selection.
    scope_model: str = SCOPE_MODEL
    data_dir: str = field(default_factory=lambda: str(DEFAULT_DATA_DIR))
    db_filename: str = "gan_fet.db"

    default_configurations: list[str] = field(
        default_factory=lambda: list(DEFAULT_CONFIGURATIONS)
    )
    default_frequencies: list[int] = field(
        default_factory=lambda: list(DEFAULT_FREQUENCIES)
    )
    default_duties: list[int] = field(default_factory=lambda: list(DEFAULT_DUTIES))
    default_temperatures: list[int] = field(
        default_factory=lambda: list(DEFAULT_TEMPERATURES)
    )
    default_voltages: list[int] = field(default_factory=lambda: list(DEFAULT_VOLTAGES))
    voltage_default_minutes: dict[str, float] = field(
        default_factory=lambda: {"200": 1, "300": 1, "400": 1}
    )

    # UI state persisted between sessions (replaces last_params.json)
    last_params: dict[str, Any] = field(default_factory=dict)
    find_zvs_before_run: bool = False
    #: Frequency search before a run. Off by default: it energises the rig
    #: while stepping the gate frequency, so it is opted into deliberately.
    find_frequency_before_run: bool = False
    #: The voltage-only ZVS sweep is retained mainly to exercise the frequency
    #: search independently, so its control is hidden unless revealed here.
    show_zvs_voltage_sweep: bool = False

    _settings_path: Path = field(
        default=DEFAULT_SETTINGS_PATH, init=False, repr=False, compare=False
    )
    _data_base_dir: Path = field(
        default=DEFAULT_CONFIG_DIR, init=False, repr=False, compare=False
    )
    _simulation_runtime: bool = field(
        default=False, init=False, repr=False, compare=False
    )

    @property
    def settings_path(self) -> Path:
        return self._settings_path

    @property
    def resolved_data_dir(self) -> Path:
        path = Path(self.data_dir).expanduser()
        if path.is_absolute():
            return path
        return (self._data_base_dir / path).resolve()

    @property
    def db_path(self) -> Path:
        return self.resolved_data_dir / self.db_filename

    @property
    def is_simulation_runtime(self) -> bool:
        """Whether every mutable runtime artifact is simulation-isolated."""
        return self._simulation_runtime

    @property
    def simulation_root(self) -> Path:
        """Directory reserved for virtual runs beside the live data tree."""
        if self._simulation_runtime:
            return self.resolved_data_dir
        return self.resolved_data_dir / SIMULATION_DIRNAME

    @property
    def simulation_db_path(self) -> Path:
        return self.simulation_root / SIMULATION_DB_FILENAME

    @property
    def screenshots_dir(self) -> Path:
        return self.resolved_data_dir / "screenshots"

    @property
    def logs_dir(self) -> Path:
        if self._simulation_runtime:
            return self.simulation_root / "logs"
        return DEFAULT_LOG_DIR

    def for_simulation(self) -> "Settings":
        """Return an independent, fail-closed settings object for simulation.

        The first virtual session starts from the live configuration so the
        operator sees the same matrix and safety limits.  Later sessions load
        their own persisted settings.  In both cases, all writable paths are
        pinned below ``<live data>/simulation`` and Google sync is forced off.
        The source object is never mutated or saved.
        """
        if self._simulation_runtime:
            # Keep this operation idempotent for callers that already prepared
            # a virtual runtime object.
            self.google.enabled = False
            return self

        root = (self.resolved_data_dir / SIMULATION_DIRNAME).resolve()
        settings_path = root / SIMULATION_SETTINGS_FILENAME
        if settings_path.is_file():
            runtime = type(self).load(settings_path)
        else:
            # Round-tripping the public payload gives us a deep, typed copy
            # without sharing mutable nested dictionaries/lists with live mode.
            runtime = type(self)._from_dict(self._payload())
            runtime.last_params = dict(runtime.last_params)
            runtime.last_params["device_name"] = SIMULATION_DEFAULT_DEVICE_NAME

        # Never trust persisted simulation paths or cloud enablement: an older
        # or hand-edited file must not escape the isolated runtime directory.
        runtime.data_dir = str(root)
        runtime.db_filename = SIMULATION_DB_FILENAME
        runtime.google.enabled = False
        runtime._settings_path = settings_path
        runtime._data_base_dir = root
        runtime._simulation_runtime = True
        return runtime

    def default_duration_minutes(self, voltage: int) -> float:
        return _positive_number(
            self.voltage_default_minutes.get(str(voltage), 1),
            1.0,
        )

    def validate_scope_configuration(self) -> None:
        """Reject any in-memory configuration outside the fixed HDO4054 rig."""
        if self.scope_model != SCOPE_MODEL:
            raise ValueError(
                "scope_model must remain fixed to the Teledyne LeCroy HDO4054"
            )
        if SCOPE_INSTRUMENT_KEY not in self.instruments:
            raise ValueError("the HDO4054 oscilloscope endpoint is missing")
        address = self.instruments[SCOPE_INSTRUMENT_KEY]
        _validate_hdo_transport(
            address.ip,
            address.port,
            key=SCOPE_INSTRUMENT_KEY,
        )
        if _LEGACY_LECROY_SCOPE_KEY in self.instruments:
            raise ValueError(
                "the legacy LeCroy endpoint must be migrated to HDO4054"
            )

    # -- persistence -------------------------------------------------

    def _payload(self) -> dict[str, Any]:
        payload = dataclasses.asdict(self)
        return {
            f.name: payload[f.name]
            for f in dataclasses.fields(self)
            if not f.name.startswith("_")
        }

    def save(self, path: Path | None = None) -> None:
        """Atomically persist settings without risking a truncated JSON file."""
        self.validate_scope_configuration()
        target = Path(path) if path is not None else self._settings_path
        target = target.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._payload(), indent=2, allow_nan=False)

        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temp_name = stream.name
                stream.write(payload)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(temp_name, 0o600)
            except OSError:
                pass
            os.replace(temp_name, target)
        finally:
            if temp_name is not None:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    pass

        self._settings_path = target
        self._data_base_dir = target.parent

    @classmethod
    def load(
        cls,
        path: Path | None = None,
        *,
        persist_legacy_migration: bool = True,
    ) -> "Settings":
        requested = Path(path) if path is not None else DEFAULT_SETTINGS_PATH
        requested = requested.expanduser().resolve()
        source = requested
        migrated = False

        if not source.is_file() and path is None:
            seen: set[Path] = {requested}
            for candidate in LEGACY_SETTINGS_PATHS:
                candidate = candidate.expanduser().resolve()
                if candidate in seen:
                    continue
                seen.add(candidate)
                if candidate.is_file():
                    source = candidate
                    migrated = True
                    break

        if not source.is_file():
            settings = cls()
            settings._settings_path = requested
            settings._data_base_dir = requested.parent
            return settings

        try:
            raw = json.loads(
                source.read_text(encoding="utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise SettingsLoadError(
                f"Could not read settings file {source}: {exc}. "
                "The file was left unchanged."
            ) from exc
        if not isinstance(raw, dict):
            raise SettingsLoadError(
                f"Settings file {source} must contain a JSON object. "
                "The file was left unchanged."
            )

        try:
            normalized = _normalize_scope_payload(
                raw, require_scope_evidence=True
            )
        except ValueError as exc:
            raise SettingsLoadError(
                f"Settings file {source} has an incompatible oscilloscope "
                f"configuration: {exc}. Confirm that the bench uses a "
                "Teledyne LeCroy HDO4054, then update or remove the settings "
                "file. The file was left unchanged."
            ) from exc

        settings = cls._from_dict(normalized)
        settings._settings_path = requested
        settings._data_base_dir = source.parent

        # External-file references are relative to the settings file, not the
        # process working directory.
        for attr in ("credentials_file", "mapping_file"):
            value = Path(getattr(settings.google, attr)).expanduser()
            if not value.is_absolute():
                setattr(
                    settings.google,
                    attr,
                    str((source.parent / value).resolve()),
                )

        # Relative paths in legacy install-local settings referred to the old
        # installation directory.  Resolve them before saving to the new
        # per-user config location so the existing DB and credentials remain
        # discoverable.
        if migrated:
            data_path = Path(settings.data_dir).expanduser()
            if not data_path.is_absolute():
                settings.data_dir = str((source.parent / data_path).resolve())
            if persist_legacy_migration:
                settings.save(requested)

        return settings

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> "Settings":
        raw = _normalize_scope_payload(raw, require_scope_evidence=False)
        settings = cls()

        instrument_raw = raw.get("instruments")
        if isinstance(instrument_raw, dict):
            merged = dict(settings.instruments)
            # Load the legacy SMU alias first so an explicit new key wins.
            ordered = sorted(
                instrument_raw.items(), key=lambda item: item[0] != "K2400"
            )
            for name, value in ordered:
                if not isinstance(value, dict):
                    continue
                canonical = "K2410" if name == "K2400" else str(name)
                try:
                    if isinstance(value.get("port"), bool):
                        raise ValueError("boolean port")
                    merged[canonical] = InstrumentAddress(
                        str(value["ip"]), int(value["port"])
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            merged.pop("K2400", None)
            settings.instruments = merged

        sections = {
            "smu": (SmuSettings, settings.smu),
            "wavegen": (WavegenSettings, settings.wavegen),
            "zvs": (ZvsSettings, settings.zvs),
            "frequency_tune": (FrequencyTuneSettings, settings.frequency_tune),
            "peak_control": (PeakControlSettings, settings.peak_control),
            "safety": (SafetySettings, settings.safety),
            "google": (GoogleSettings, settings.google),
        }
        for name, (section_type, fallback) in sections.items():
            if name in raw:
                section_value = raw[name]
                if name == "smu":
                    section_value = _normalize_smu_mapping(section_value)
                setattr(
                    settings,
                    name,
                    _dataclass_from_mapping(
                        section_type,
                        section_value,
                        fallback,
                    ),
                )

        if isinstance(raw.get("data_dir"), str) and raw["data_dir"].strip():
            settings.data_dir = raw["data_dir"]
        if isinstance(raw.get("db_filename"), str) and raw["db_filename"].strip():
            filename = raw["db_filename"].strip()
            if (
                filename not in {".", ".."}
                and "/" not in filename
                and "\\" not in filename
            ):
                settings.db_filename = filename

        list_fields: dict[str, type] = {
            "default_configurations": str,
            "default_frequencies": int,
            "default_duties": int,
            "default_temperatures": int,
            "default_voltages": int,
        }
        for name, value_type in list_fields.items():
            value = raw.get(name)
            if not isinstance(value, list):
                continue
            try:
                if any(isinstance(item, bool) for item in value):
                    raise ValueError("boolean matrix value")
                converted = [value_type(item) for item in value]
            except (TypeError, ValueError):
                continue
            setattr(settings, name, converted)

        durations = raw.get("voltage_default_minutes")
        if isinstance(durations, dict):
            try:
                settings.voltage_default_minutes = {
                    str(key): float(value) for key, value in durations.items()
                }
            except (TypeError, ValueError):
                pass
        if isinstance(raw.get("last_params"), dict):
            settings.last_params = dict(raw["last_params"])
        for flag in (
            "find_zvs_before_run",
            "find_frequency_before_run",
            "show_zvs_voltage_sweep",
        ):
            if isinstance(raw.get(flag), bool):
                setattr(settings, flag, raw[flag])

        cls._validate_numeric_settings(settings)
        return settings

    @staticmethod
    def _validate_numeric_settings(settings: "Settings") -> None:
        """Restore safe defaults for malformed or non-positive numeric values."""
        defaults = Settings()
        positive_fields = {
            "smu": (
                "max_voltage_v",
                "current_compliance_a",
                "nplc",
                "sample_interval_s",
                "ramp_step_v",
                "ramp_rate_v_s",
            ),
            "wavegen": (
                "freq_ramp_rate_khz_s",
                "duty_ramp_rate_pct_s",
                "duty_step_pct",
            ),
            "zvs": (
                "step_v",
                "settle_s",
                "samples_per_point",
                "min_improvement_a",
                "window_v",
                "max_steps",
                "autotune_freq_rate_khz_s",
            ),
            "peak_control": (
                "tolerance_v",
                "settle_s",
                "max_iterations",
                "min_step_v",
                "proportional_gain",
                "secant_min_delta_v",
                "secant_max_step_v",
            ),
            "frequency_tune": (
                "window_frac",
                "survey_window_frac",
                "survey_step_hz",
                "survey_peak_frac",
                "coarse_step_hz",
                "fine_step_hz",
                "fine_span_hz",
                "warm_window_frac",
                "settle_s",
                "soft_band_v",
                "ceiling_margin_frac",
                "gain_growth_factor",
                "anomaly_ratio",
                "zvs_threshold_frac",
                "max_points",
            ),
            "safety": (
                "max_dc_current_a",
                "max_vds_peak_v",
                "watchdog_consecutive_failures",
            ),
        }
        integer_fields = {
            ("zvs", "samples_per_point"),
            ("zvs", "max_steps"),
            ("peak_control", "max_iterations"),
            ("frequency_tune", "max_points"),
            ("safety", "watchdog_consecutive_failures"),
        }
        for section_name, field_names in positive_fields.items():
            section = getattr(settings, section_name)
            default_section = getattr(defaults, section_name)
            for field_name in field_names:
                value = getattr(section, field_name)
                fallback = getattr(default_section, field_name)
                normalized = _positive_number(
                    value,
                    fallback,
                    integer=(section_name, field_name) in integer_fields,
                )
                if normalized != value:
                    log.warning(
                        "Invalid setting %s.%s=%r; using safe default %r",
                        section_name,
                        field_name,
                        value,
                        fallback,
                    )
                setattr(section, field_name, normalized)

        if settings.safety.max_vds_peak_v < 1.0:
            log.warning(
                "Safety Vds limit must permit at least a 1 V target; using %g V",
                defaults.safety.max_vds_peak_v,
            )
            settings.safety.max_vds_peak_v = defaults.safety.max_vds_peak_v

        gpib = settings.smu.prologix_gpib_addr
        if gpib is not None:
            if (
                isinstance(gpib, bool)
                or not isinstance(gpib, int)
                or not 0 <= gpib <= 30
            ):
                log.warning(
                    "Invalid setting smu.prologix_gpib_addr=%r; disabling it",
                    gpib,
                )
                settings.smu.prologix_gpib_addr = None

        for name, address in list(settings.instruments.items()):
            target = address.ip.strip() if isinstance(address.ip, str) else ""
            is_uri = "://" in target
            is_visa = target.upper().startswith("GPIB") or "::" in target
            is_serial = target.upper().startswith("COM") or target.startswith("/")
            if is_uri or is_visa:
                port_valid = isinstance(address.port, int) and not isinstance(
                    address.port, bool
                ) and 0 <= address.port <= 4_000_000
            elif is_serial:
                port_valid = isinstance(address.port, int) and not isinstance(
                    address.port, bool
                ) and 0 < address.port <= 4_000_000
            else:
                port_valid = isinstance(address.port, int) and not isinstance(
                    address.port, bool
                ) and 0 < address.port <= 65535
            if (
                not target
                or not port_valid
            ):
                fallback = defaults.instruments.get(name)
                if fallback is None:
                    del settings.instruments[name]
                else:
                    settings.instruments[name] = dataclasses.replace(fallback)

        # A VISA resource already performs GPIB addressing. Older settings and
        # the pre-refactor editor could persist both modes at once, causing
        # Prologix controller commands to be sent directly to the Keithley.
        smu_address = next(
            (
                address
                for name, address in settings.instruments.items()
                if name.upper().startswith("K24")
                or "KEITHLEY" in name.upper()
                or "SMU" in name.upper()
            ),
            None,
        )
        if smu_address is not None:
            target = smu_address.ip.strip()
            if target.upper().startswith("GPIB") or "::" in target:
                if settings.smu.prologix_gpib_addr is not None:
                    log.warning(
                        "Ignoring smu.prologix_gpib_addr for direct VISA resource %s",
                        target,
                    )
                settings.smu.prologix_gpib_addr = None

        google = settings.google
        default_google = defaults.google
        if not isinstance(google.enabled, bool):
            google.enabled = False
        for attr in ("credentials_file", "mapping_file"):
            value = getattr(google, attr)
            if not isinstance(value, str) or not value.strip():
                setattr(google, attr, getattr(default_google, attr))
        for attr in ("project_folder_id", "master_spreadsheet_id"):
            value = getattr(google, attr)
            if not isinstance(value, str):
                setattr(google, attr, getattr(default_google, attr))

        settings.voltage_default_minutes = {
            str(voltage): _positive_number(value, 1.0)
            for voltage, value in settings.voltage_default_minutes.items()
        }

        configurations = [
            value
            for value in settings.default_configurations
            if value in DEFAULT_CONFIGURATIONS
        ]
        settings.default_configurations = (
            list(dict.fromkeys(configurations))
            if configurations
            else list(DEFAULT_CONFIGURATIONS)
        )

        def valid_ints(
            values: list[Any],
            fallback: list[int],
            predicate,
        ) -> list[int]:
            cleaned = [
                value
                for value in values
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and predicate(value)
                )
            ]
            return list(dict.fromkeys(cleaned)) or list(fallback)

        settings.default_frequencies = valid_ints(
            settings.default_frequencies,
            DEFAULT_FREQUENCIES,
            lambda value: 1 <= value <= 500_000_000,
        )
        settings.default_duties = valid_ints(
            settings.default_duties,
            DEFAULT_DUTIES,
            lambda value: 0.1 <= value <= 99.9,
        )
        voltage_limit = float(settings.safety.max_vds_peak_v)
        voltage_fallback = [
            value for value in DEFAULT_VOLTAGES if value <= voltage_limit
        ]
        if not voltage_fallback:
            voltage_fallback = [max(1, int(math.floor(voltage_limit)))]
        settings.default_voltages = valid_ints(
            settings.default_voltages,
            voltage_fallback,
            lambda value: (
                0 < value <= voltage_limit
            ),
        )
        settings.default_temperatures = valid_ints(
            settings.default_temperatures,
            DEFAULT_TEMPERATURES,
            lambda _value: True,
        )
