"""Toolkit-free presentation policy shared by every UI front-end.

The mode banner is a safety identity, not decoration: both the Tk and the Qt
window must show byte-identical text so an operator can never mistake a
simulation for the bench. Keeping the policy here means neither toolkit owns
it.
"""

from __future__ import annotations


def mode_banner_presentation(is_simulated: bool) -> tuple[str, str]:
    """Return the persistent operator-facing mode identity and colour."""
    if is_simulated:
        return (
            "SIMULATION — VIRTUAL INSTRUMENTS — NO BENCH I/O — "
            "NOT MEASURED DATA",
            "#6a1b9a",
        )
    return ("LIVE HARDWARE — REAL BENCH OUTPUTS", "#b71c1c")


#: Operator-facing names for the operations that need the hardware online.
#: Keyed by operation kind; both front-ends quote these in refusals.
HARDWARE_OPERATION_LABELS = {
    "apply_wavegen": "apply wavegen settings",
    "autotune": "recall the tuned frequency",
    "bus_off": "control the bus output",
    "experiment": "start an experiment",
    "reset_safety": "reset the safety interlock",
    "sequence": "start an auto sequence",
    "simulation_validation": "start the simulated validation run",
    "voltage_tune": "tune the DC voltage",
}
