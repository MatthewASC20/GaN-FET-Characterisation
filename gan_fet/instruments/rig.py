"""Composition of the bench instruments into one application-facing rig.

Instrument drivers should not know how configuration keys are named and the
GUI should not know how transports are constructed.  This module is the
single boundary between persisted settings and the hardware abstraction
interfaces used by the experiment engine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from gan_fet.core.autotune import WavegenController
from gan_fet.instruments.base import (
    MultimeterInterface,
    OscilloscopeInterface,
    SmuInterface,
    WavegenInterface,
)
from gan_fet.instruments.multimeter import Sdm3055
from gan_fet.instruments.oscilloscope import LeCroyHdo4054
from gan_fet.instruments.smu import Keithley2410
from gan_fet.instruments.wavegen import Sdg6022x
from gan_fet.settings import SCOPE_INSTRUMENT_KEY, Settings

log = logging.getLogger(__name__)


class RigConfigurationError(ValueError):
    """Required instrument settings are missing or ambiguous."""


@dataclass
class InstrumentRig:
    """All instruments and controllers required for one application run."""

    wavegen: WavegenInterface
    scope: OscilloscopeInterface
    dmm: Optional[MultimeterInterface]
    smu: SmuInterface
    wavegen_controller: WavegenController
    clients: tuple[Any, ...]

    def close_clients(self) -> None:
        """Best-effort close of every unique underlying client."""
        for client in self.clients:
            try:
                client.close()
            except Exception:
                log.exception("Could not close instrument client")



def _first_configured(settings: Settings, names: Iterable[str], role: str) -> str:
    for name in names:
        if name and name in settings.instruments:
            return name
    raise RigConfigurationError(
        f"No configured {role} instrument. Checked: {', '.join(filter(None, names))}"
    )


def resolve_instrument_roles(settings: Settings) -> dict[str, str]:
    """Resolve the fixed HDO4054 rig roles from validated settings."""
    try:
        settings.validate_scope_configuration()
    except ValueError as exc:
        raise RigConfigurationError(str(exc)) from exc
    scope_key = _first_configured(
        settings,
        (SCOPE_INSTRUMENT_KEY,),
        f"{SCOPE_INSTRUMENT_KEY} oscilloscope",
    )
    return {
        "wavegen": _first_configured(
            settings, ("SDG6022X",), "waveform generator"
        ),
        "scope": scope_key,
        "smu": _first_configured(settings, ("K2410",), "SMU"),
    }


def build_instrument_rig(settings: Settings, *, simulate: bool = False) -> InstrumentRig:
    """Build one live or simulated rig from validated settings.

    Simulation clients share one electrical plant, while live clients resolve
    their own TCP, serial, VISA, or Prologix transport.  Any partial build is
    closed before the error is re-raised.
    """
    from gan_fet.instruments.mock_scpi import MockScpiTcpClient, SimulatedRigPlant
    from gan_fet.instruments.scpi import ScpiTcpClient

    roles = resolve_instrument_roles(settings)
    plant = SimulatedRigPlant(resonant_model=True) if simulate else None
    clients: list[Any] = []

    def make_client(name: str) -> Any:
        if simulate:
            created: Any = MockScpiTcpClient(name, shared_plant=plant)
            log.info("Using virtual instrument %s", name)
        else:
            address = settings.instruments[name]
            created = ScpiTcpClient(name, address.ip, address.port)
            description = getattr(getattr(created, "transport", None), "description", None)
            log.info("Instrument %s resolved to %s", name, description or address.ip)
        clients.append(created)
        return created

    try:
        wavegen = Sdg6022x(make_client(roles["wavegen"]))
        scope = LeCroyHdo4054(make_client(roles["scope"]))
        dmm = (
            Sdm3055(make_client("SDM3055"))
            if "SDM3055" in settings.instruments
            else None
        )
        smu = Keithley2410(make_client(roles["smu"]), settings.smu)
        # The controller gets the scope and a ramp ceiling below the interlock,
        # so a standalone frequency move is halted before a trip rather than
        # after one. Sits under the hard ceiling by the same margin the
        # frequency search uses.
        controller = WavegenController(
            wavegen,
            settings.wavegen,
            scope=scope,
            peak_ceiling_v=(
                settings.safety.max_vds_peak_v
                * settings.frequency_tune.ceiling_margin_frac
            ),
        )
    except Exception:
        for client in clients:
            try:
                client.close()
            except Exception:
                pass
        raise

    return InstrumentRig(
        wavegen=wavegen,
        scope=scope,
        dmm=dmm,
        smu=smu,
        wavegen_controller=controller,
        clients=tuple(dict.fromkeys(clients)),
    )
