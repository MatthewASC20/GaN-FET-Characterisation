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
    InstrumentFactory,
    MultimeterInterface,
    OscilloscopeInterface,
    SmuInterface,
    WavegenInterface,
)
from gan_fet.instruments.multimeter import Sdm3055
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

    def legacy_tuple(self) -> tuple[
        WavegenInterface,
        OscilloscopeInterface,
        Optional[MultimeterInterface],
        SmuInterface,
        WavegenController,
    ]:
        """Compatibility shape used by older composition/tests."""
        return (
            self.wavegen,
            self.scope,
            self.dmm,
            self.smu,
            self.wavegen_controller,
        )


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
    smu_candidates = (
        "K2410",
        "K2400",
        "Keithley2410",
        "Keithley2400",
        *(
            name
            for name in settings.instruments
            if name.upper().startswith("K24")
            or "KEITHLEY" in name.upper()
            or "SMU" in name.upper()
        ),
    )
    return {
        "wavegen": _first_configured(
            settings, ("SDG6022X",), "waveform generator"
        ),
        "scope": scope_key,
        "smu": _first_configured(settings, smu_candidates, "SMU"),
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

    def make_client(name: str, *, is_smu: bool = False) -> Any:
        if simulate:
            created: Any = MockScpiTcpClient(name, shared_plant=plant)
            log.info("Using virtual instrument %s", name)
        else:
            address = settings.instruments[name]
            created = ScpiTcpClient(
                name,
                address.ip,
                address.port,
                prologix_addr=(
                    settings.smu.prologix_gpib_addr if is_smu else None
                ),
            )
            description = getattr(getattr(created, "transport", None), "description", None)
            log.info("Instrument %s resolved to %s", name, description or address.ip)
        clients.append(created)
        return created

    try:
        wavegen = InstrumentFactory.create_wavegen(make_client(roles["wavegen"]))
        scope = InstrumentFactory.create_oscilloscope(make_client(roles["scope"]))
        dmm = (
            Sdm3055(make_client("SDM3055"))
            if "SDM3055" in settings.instruments
            else None
        )
        smu = InstrumentFactory.create_smu(
            make_client(roles["smu"], is_smu=True), settings.smu
        )
        controller = WavegenController(wavegen, settings.wavegen)
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
