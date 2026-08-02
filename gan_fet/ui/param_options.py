"""Per-device parameter option sets: cleaning, labelling and ordering.

Option sets arrive from the database as free-form JSON that an earlier release
wrote, so entries may be bare values, ``(value, label)`` pairs or dictionaries,
and any of them may be malformed. Normalising that is pure data work with one
rig-dependent input — the Vds peak ceiling — and it lived inside the main
window only because that is where the editors happen to be drawn.

No tkinter here: this is the decision layer, testable without a display.

An invalid entry is dropped rather than rejected wholesale. A single bad option
in a stored set should not make a device unusable, and the operator sees the
survivors in the editor.
"""

from __future__ import annotations

from typing import Any, Optional

from gan_fet.core.models import freq_label
from gan_fet.settings import DEFAULT_CONFIGURATIONS

#: The parameter axes a device carries its own option set for.
OPTION_KEYS = ("configurations", "frequencies", "duties", "temperatures", "voltages")

#: Bounds that do not depend on rig settings. Frequency is the driver's own
#: safe range; duty excludes the degenerate 0% and 100% cases.
FREQUENCY_RANGE_HZ = (1, 500_000_000)
DUTY_RANGE_PCT = (1, 99)


def default_label(key: str, value: Any) -> str:
    """Human label for an option that was stored without one."""
    if key == "frequencies":
        return freq_label(value)
    if key == "duties":
        return f"{value}%"
    if key == "temperatures":
        return f"{value}°C"
    if key == "voltages":
        return f"{value}V"
    return str(value)


def _unpack(entry: Any) -> tuple[Any, str]:
    """Read one stored entry in any of the shapes releases have written."""
    if isinstance(entry, dict):
        return entry.get("value"), str(entry.get("label", ""))
    if isinstance(entry, (list, tuple)) and entry:
        return entry[0], str(entry[1]) if len(entry) > 1 else ""
    return entry, ""


def _accept_configuration(value: Any, seen: set) -> Optional[str]:
    text = str(value).strip()
    if text not in DEFAULT_CONFIGURATIONS or text in seen:
        return None
    return text


def _accept_numeric(
    key: str, value: Any, seen: set, max_vds_peak_v: float
) -> Optional[int]:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if key == "frequencies" and not (
        FREQUENCY_RANGE_HZ[0] <= number <= FREQUENCY_RANGE_HZ[1]
    ):
        return None
    if key == "duties" and not (DUTY_RANGE_PCT[0] <= number <= DUTY_RANGE_PCT[1]):
        return None
    # A target above the interlock could never be reached, so offering it would
    # only produce a run that fails once energised.
    if key == "voltages" and not 0 < number <= max_vds_peak_v:
        return None
    if number in seen:
        return None
    return number


def normalize_options(
    key: str,
    raw: Optional[list],
    *,
    max_vds_peak_v: float,
) -> list[tuple[Any, str]]:
    """Return usable ``(value, label)`` options for one parameter axis.

    Configurations sort by label because they are named; every other axis sorts
    by value because they are magnitudes and an operator reads them in order.
    """
    if not raw:
        return []

    cleaned: list[tuple[Any, str]] = []
    seen: set = set()
    for entry in raw:
        value, label = _unpack(entry)
        if key == "configurations":
            accepted: Any = _accept_configuration(value, seen)
        else:
            accepted = _accept_numeric(key, value, seen, max_vds_peak_v)
        if accepted is None:
            continue
        seen.add(accepted)
        cleaned.append((accepted, label.strip() or default_label(key, accepted)))

    cleaned.sort(
        key=(lambda e: str(e[1]).lower())
        if key == "configurations"
        else (lambda e: e[0])
    )
    return cleaned
