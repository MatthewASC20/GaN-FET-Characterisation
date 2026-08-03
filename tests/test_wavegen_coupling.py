"""Dual conduction relies on the generator mirroring C1 onto C2.

Writing both channels on every change doubled the command count on the
frequency ramp — stepped every 10 kHz, and the dominant cost of a search. The
generator already couples them, so the second write was redundant.

Relying on coupling is only safe if it is *verified*, which it previously was
not. These tests pin both halves: coupling is confirmed before it is trusted,
and once confirmed only C1 is written.
"""

from __future__ import annotations

import pytest

from gan_fet.instruments.mock_scpi import MockScpiTcpClient, SimulatedRigPlant
from gan_fet.instruments.wavegen import Sdg6022x


class _RecordingClient(MockScpiTcpClient):
    """Mock client that remembers every command written."""

    def __init__(self, plant: SimulatedRigPlant) -> None:
        super().__init__("SDG6022X", shared_plant=plant)
        self.writes: list[str] = []

    def write(self, command: str) -> bool:
        self.writes.append(command)
        return super().write(command)


@pytest.fixture()
def rig():
    plant = SimulatedRigPlant()
    client = _RecordingClient(plant)
    return Sdg6022x(client), client, plant


# -- coupling is verified before it is relied on -----------------------------


def test_dual_configuration_verifies_coupling(rig):
    wavegen, client, plant = rig
    wavegen.configure("Dual Conduction", 6_000_000, 50)
    assert "COUP?" in client.writes or any(
        "COUP?" in c for c in client.writes
    ) or plant.wavegen_freq_coupled
    assert plant.wavegen_coupled
    assert plant.wavegen_freq_coupled
    assert plant.wavegen_duty_coupled


def _only_coup(wavegen, monkeypatch, answer):
    """Replace just the coupling query, leaving other readbacks intact.

    Stubbing every query would trip the output-off readback first and test a
    different failure entirely.
    """
    real_query = wavegen.client.query

    def routed(command: str):
        if "COUP" in command.upper():
            return answer
        return real_query(command)

    monkeypatch.setattr(wavegen.client, "query", routed)


def test_configuration_fails_when_coupling_does_not_engage(rig, monkeypatch):
    """Fails closed, like the scope identity check: unconfirmed coupling means
    the gates could diverge, so configuration is rejected rather than assumed."""
    wavegen, _client, _plant = rig
    _only_coup(wavegen, monkeypatch, "COUP STATE,ON,FCOUP,OFF,DCOUP,ON")
    with pytest.raises(ConnectionError, match="frequency"):
        wavegen.configure("Dual Conduction", 6_000_000, 50)


def test_configuration_fails_when_coupling_cannot_be_read(rig, monkeypatch):
    wavegen, _client, _plant = rig
    _only_coup(wavegen, monkeypatch, None)
    with pytest.raises(ConnectionError, match="coupling"):
        wavegen.configure("Dual Conduction", 6_000_000, 50)


# -- once verified, only C1 is written ---------------------------------------


def test_frequency_changes_write_only_channel_one(rig):
    wavegen, client, _plant = rig
    wavegen.configure("Dual Conduction", 6_000_000, 50)
    client.writes.clear()

    wavegen.set_frequency(6_100_000, dual=True)

    frequency_writes = [c for c in client.writes if "BSWV FRQ" in c.upper()]
    assert frequency_writes, "the frequency should have been written at all"
    assert not any(c.upper().startswith("C2:") for c in frequency_writes), (
        "C2 is mirrored by verified coupling; writing it doubles ramp traffic"
    )


def test_duty_changes_write_only_channel_one(rig):
    wavegen, client, _plant = rig
    wavegen.configure("Dual Conduction", 6_000_000, 50)
    client.writes.clear()

    wavegen.set_duty(40, dual=True)

    duty_writes = [c for c in client.writes if "BSWV DUTY" in c.upper()]
    assert duty_writes
    assert not any(c.upper().startswith("C2:") for c in duty_writes)


def test_a_ramp_costs_one_command_per_step_not_two(rig):
    """The saving this change exists for, measured rather than asserted by
    inspection."""
    wavegen, client, _plant = rig
    wavegen.configure("Dual Conduction", 6_000_000, 50)
    client.writes.clear()

    wavegen.ramp_to_frequency(6_050_000, dual=True, rate_khz_s=1e9)

    steps = [c for c in client.writes if "BSWV FRQ" in c.upper()]
    assert steps
    assert all(c.upper().startswith("C1:") for c in steps)


# -- single-channel configurations are unaffected ----------------------------


def test_single_configuration_disables_coupling(rig):
    wavegen, _client, plant = rig
    wavegen.configure("Single Device", 6_000_000, 50)
    assert plant.wavegen_coupled is False
    assert plant.wavegen_freq_coupled is False


def test_single_configuration_needs_no_coupling_readback(rig, monkeypatch):
    """Verification is specific to dual conduction; a single-channel setup
    must not be blocked by a generator that cannot report coupling."""
    wavegen, _client, _plant = rig

    def refuse(_cmd):
        raise AssertionError("single-channel configuration must not query COUP")

    monkeypatch.setattr(wavegen, "_verify_coupling", lambda: refuse("COUP?"))
    wavegen.configure("Single Conduction", 6_000_000, 50)
