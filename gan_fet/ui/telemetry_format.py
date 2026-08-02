"""Formatting for the live telemetry readouts.

Separated from the widgets so the unit choices — which are the part an operator
actually reads off the screen mid-run — can be checked without a display.

Every formatter renders "no reading" as an em dash rather than a zero. A zero
is a measurement; a missing value is not, and on a rig where zero current means
something specific the two must not look alike.
"""

from __future__ import annotations

import math
from typing import Optional

#: Shown when a value is unavailable. Deliberately not "0".
NO_READING = "—"


def _finite(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def format_frequency(freq_hz: Optional[float]) -> str:
    """Render a switching frequency in whichever unit reads naturally."""
    value = _finite(freq_hz)
    if value is None or value <= 0:
        return f"{NO_READING} MHz"
    if value >= 1e6:
        return f"{value / 1e6:.2f} MHz"
    return f"{value / 1e3:.1f} kHz"


def format_volts(volts: Optional[float]) -> str:
    value = _finite(volts)
    return f"{NO_READING} V" if value is None else f"{value:.1f} V"


def format_milliamps(amps: Optional[float]) -> str:
    """DC input current, shown in mA: the working range is tens of mA."""
    value = _finite(amps)
    return f"{NO_READING} mA" if value is None else f"{value * 1000.0:.2f} mA"


def format_amps(amps: Optional[float]) -> str:
    """RMS currents, shown in A: these run to several amps."""
    value = _finite(amps)
    return f"{NO_READING} A" if value is None else f"{value:.3f} A"


def last_current_text(amps: Optional[float]) -> str:
    """The headline "Last Current" readout.

    Six decimal places rather than the card's three: this is the number an
    operator watches for the small changes that say a frequency step helped,
    and rounding it to milliamps hides exactly that.
    """
    value = _finite(amps)
    reading = NO_READING if value is None else f"{value:.6f} A"
    return f"Last Current: {reading}"
