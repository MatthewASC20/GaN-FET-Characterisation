"""Assembling a run request from operator input.

Turning what is on screen into an :class:`ExperimentParams` is a validation
problem, not a widget problem: a device name has to be filesystem-safe, every
parameter axis needs at least one option, the numeric fields have to parse, and
the duration has to be finite and positive.

That decision lived inside the window, interleaved with ``messagebox`` calls,
which made it untestable without a display. Here the checks raise
:class:`InputRejected` carrying the title and message, and the window catches
it in one place and shows the dialog. The dialogs stay in the window — where
the tests patch ``messagebox`` — and the reasoning becomes testable anywhere.

No tkinter: callers read their own widget values and pass plain data.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Sequence

from gan_fet.core.models import ExperimentParams, MatrixPoint, sanitize_device_name

#: Longest run this UI will accept, as a guard against a mistyped duration
#: leaving the rig energised overnight.
MAX_DURATION_MINUTES = 24 * 60

#: Operator-facing names for the parameter axes, used when reporting which
#: option sets are empty.
PARAMETER_LABELS: dict[str, str] = {
    "configurations": "Configuration",
    "frequencies": "Frequency",
    "duties": "Duty Cycle",
    "temperatures": "Temperature",
    "voltages": "Voltage",
}


class InputRejected(ValueError):
    """Operator input cannot be turned into a run, with the reason to show."""

    def __init__(self, title: str, message: str) -> None:
        super().__init__(f"{title}: {message}")
        self.title = title
        self.message = message


def parse_positive_duration(
    raw: str, *, maximum_minutes: float = MAX_DURATION_MINUTES
) -> float:
    """Parse one finite, positive experiment duration."""
    value = float(raw)
    if not math.isfinite(value) or value <= 0 or value > maximum_minutes:
        raise ValueError(
            f"duration must be finite and between 0 and {maximum_minutes:g} minutes"
        )
    return value


def validated_device_name(raw: str) -> str:
    """Return the device name, rejecting anything the sanitiser would alter.

    Silently sanitising would write results under a name the operator did not
    type, so a difference is reported rather than corrected.
    """
    stripped = raw.strip()
    sanitised = sanitize_device_name(stripped)
    if not sanitised or sanitised != stripped:
        raise InputRejected(
            "Input Error",
            "Enter a device name using only letters, numbers, spaces, "
            "'_' or '-'.",
        )
    return sanitised


def require_populated_options(
    available: Mapping[str, Sequence[Any]], keys: Sequence[str]
) -> None:
    """Reject a run when any parameter axis has no options to choose from."""
    empty = [key for key in keys if not available.get(key)]
    if not empty:
        return
    missing = ", ".join(PARAMETER_LABELS.get(key, key) for key in empty)
    raise InputRejected(
        "Missing Parameters",
        "Cannot proceed. The following parameter(s) have no options "
        f"defined: {missing}",
    )


def build_matrix_point(
    *,
    device_name: str,
    config: str,
    frequency_hz: Any,
    duty_pct: Any,
    temperature_c: Any,
    voltage_v: Any,
) -> MatrixPoint:
    """Assemble the selected matrix point, or reject the selection."""
    try:
        return MatrixPoint(
            device_name=validated_device_name(device_name),
            config=config,
            frequency_hz=int(frequency_hz),
            duty_pct=int(duty_pct),
            temperature_c=int(temperature_c),
            voltage_v=int(voltage_v),
        )
    except InputRejected:
        raise
    except (TypeError, ValueError) as exc:
        raise InputRejected(
            "Input Error", "Invalid parameter selection."
        ) from exc


def build_experiment_params(
    point: MatrixPoint,
    duration_text: str,
    *,
    tune_voltage: bool,
    tune_frequency: bool,
) -> ExperimentParams:
    """Attach a validated duration and the search flags to a matrix point."""
    try:
        duration = parse_positive_duration(duration_text)
    except (TypeError, ValueError) as exc:
        raise InputRejected(
            "Input Error",
            f"Please enter a finite, positive duration.\n\n{exc}",
        ) from exc
    return ExperimentParams(
        point=point,
        duration_minutes=duration,
        tune_voltage=tune_voltage,
        tune_frequency=tune_frequency,
    )


def tuning_candidate(
    prior: Optional[tuple[float, str, int]],
    applied_freq_hz: Optional[float],
    *,
    tolerance_hz: float = 1.0,
) -> Optional[tuple[float, str, int]]:
    """Whether a prior tuned frequency is worth ramping to.

    Offering a tune to where the generator already sits would present an
    action that does nothing, so a match within the generator's own resolution
    is treated as no candidate at all.
    """
    if prior is None:
        return None
    if applied_freq_hz is not None and abs(applied_freq_hz - prior[0]) <= tolerance_hz:
        return None
    return prior
