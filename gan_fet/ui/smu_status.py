"""The SMU status line.

One line carrying the three things an operator checks before touching
anything: what the bus is commanded to, what current is flowing, and whether
the output is live. Kept tkinter-free so the wording — particularly the
distinction between a commanded setpoint and a measured current — can be
tested without a display.
"""

from __future__ import annotations

from typing import Optional

from gan_fet.ui.telemetry_format import NO_READING, format_milliamps

#: Shown for the output state. Spelled out rather than a colour or an icon:
#: this is the field that says whether the rig is energised.
OUTPUT_ON = "ON"
OUTPUT_OFF = "OFF"


def smu_status_line(
    *,
    setpoint_v: Optional[float],
    current_a: Optional[float],
    output_on: bool,
) -> str:
    """Render the status line.

    The voltage is labelled a setpoint on purpose. It is what the SMU was
    commanded to, not what the scope measured across the device, and the two
    diverge under load — reading it as a measurement is how a compliance limit
    gets mistaken for a working bus.
    """
    voltage = f"{setpoint_v:.1f} V" if setpoint_v is not None else f"{NO_READING} V"
    current = format_milliamps(current_a) if current_a is not None else NO_READING
    state = OUTPUT_ON if output_on else OUTPUT_OFF
    return f"Bus setpoint: {voltage} | I: {current} | Output: {state}"
