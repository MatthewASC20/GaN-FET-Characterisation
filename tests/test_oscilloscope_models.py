"""HDO4054 identity, MAUI measurement, and screenshot regressions."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Optional

import pytest

from gan_fet.instruments.mock_scpi import MockScpiTcpClient, SimulatedRigPlant
from gan_fet.instruments.oscilloscope import LeCroyHdo4054
from gan_fet.scpi import ScpiClient
from gan_fet.transport.visa_resource import VisaTransport


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _valid_png() -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    scanline = b"\x00\x00\x00\x00\xff"
    return (
        signature
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(scanline))
        + _png_chunk(b"IEND", b"")
    )


class FakeScopeVisaResource:
    def __init__(self, binary: bytes) -> None:
        self.binary = binary
        self.timeout = 5000
        self.read_termination: str | None = "\n"
        self.operations: list[tuple[str, object]] = []
        self.binary_session_state: list[tuple[int, str | None]] = []
        self.closed = False

    def write(self, command: str) -> int:
        self.operations.append(("write", command))
        return len(command)

    def query(self, command: str) -> str:
        self.operations.append(("query", command))
        return "1"

    def read(self) -> str:
        return "1"

    def read_bytes(
        self,
        count: int,
        *,
        break_on_termchar: bool,
    ) -> bytes:
        assert break_on_termchar
        self.operations.append(("read-bytes", count))
        self.binary_session_state.append((self.timeout, self.read_termination))
        return self.binary[:count]

    def close(self) -> None:
        self.closed = True


def _configured_scope_client(
    resource: FakeScopeVisaResource,
) -> tuple[ScpiClient, VisaTransport]:
    exact_address = "TCPIP0::scope.example::inst0::INSTR"
    transport = VisaTransport(exact_address)
    transport._visa = resource
    return ScpiClient("HDO4054", transport), transport


class RecordingSession:
    """Small transport-neutral session used to assert the HDO dialect."""

    name = "HDO4054"
    is_simulated = True

    def __init__(
        self,
        values: Optional[dict[str, float]] = None,
        responses: Optional[dict[str, Optional[str]]] = None,
    ) -> None:
        self.values = values or {}
        self.responses = responses or {}
        self.queries: list[str] = []

    def write(self, _command: str) -> bool:
        return True

    def query(self, command: str) -> Optional[str]:
        self.queries.append(command)
        return self.responses.get(command)

    def query_float(self, command: str) -> Optional[float]:
        self.queries.append(command)
        return self.values.get(command)

    def request_local_control(self) -> bool:
        return True


class RecordingScreenshotAdapter:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.destination: str | None = None

    def print_screen(self, destination: str) -> None:
        self.destination = destination
        Path(destination).write_bytes(self.payload)


def test_hdo4054_uses_exact_vbs_parameter_slots() -> None:
    commands = (
        "VBS? 'return=app.Measure.P1.Out.Result.Value'",
        "VBS? 'return=app.Measure.P2.Out.Result.Value'",
        "VBS? 'return=app.Measure.P3.Out.Result.Value'",
    )
    session = RecordingSession(dict(zip(commands, (399.0, 2.4, 1.1))))
    scope = LeCroyHdo4054(session)

    assert scope.peak_voltage() == 399.0
    assert scope.rms_current() == 2.4
    assert scope.isw_rms() == 1.1
    assert session.queries == list(commands)


def test_hdo4054_does_not_fallback_to_an_unparseable_statistics_query() -> None:
    command = "VBS? 'return=app.Measure.P1.Out.Result.Value'"
    session = RecordingSession()
    scope = LeCroyHdo4054(session)

    assert scope.peak_voltage() is None
    assert session.queries == [command]


@pytest.mark.parametrize(
    "identity",
    (
        "LECROY,HDO4054,HDO000001,8.3.0",
        "*IDN LECROY,HDO4054,HDO000001,8.3.0",
        "  *idn lecroy,hdo4054,HDO000001,8.3.0  ",
    ),
)
def test_hdo4054_identity_accepts_optional_command_header(identity: str) -> None:
    session = RecordingSession(responses={"*IDN?": identity})
    scope = LeCroyHdo4054(session)

    assert scope.verify_identity() == identity.strip()
    assert session.queries == ["*IDN?"]


@pytest.mark.parametrize(
    "identity",
    (
        None,
        "",
        "LECROY",
        "OTHER,HDO4054,1001,1.0",
        "LECROY,HDO4054A,1001,1.0",
        "LECROY,HDO4054-MS,1001,1.0",
        "*IDN? LECROY,HDO4054,1001,1.0",
    ),
)
def test_hdo4054_identity_rejects_missing_or_different_instrument(
    identity: Optional[str],
) -> None:
    session = RecordingSession(responses={"*IDN?": identity})
    scope = LeCroyHdo4054(session)

    with pytest.raises(ConnectionError):
        scope.verify_identity()
    assert session.queries == ["*IDN?"]


def test_mock_hdo4054_has_valid_identity_and_maui_measurements() -> None:
    plant = SimulatedRigPlant()
    plant.smu_output_on = True
    plant.smu_voltage_setpoint_v = 100.0
    plant.wavegen_c1_on = True
    scope = LeCroyHdo4054(MockScpiTcpClient("HDO4054", plant))

    assert scope.verify_identity() == "*IDN LECROY,HDO4054,HDO4054SIM,1.0"
    assert scope.peak_voltage() == 300.0
    assert scope.rms_current() == 2.5
    assert scope.isw_rms() == 1.2


def test_mock_hdo4054_currents_follow_shared_plant_state() -> None:
    plant = SimulatedRigPlant()
    client = MockScpiTcpClient("HDO4054", plant)
    scope = LeCroyHdo4054(client)

    assert scope.rms_current() == 0.0
    assert scope.isw_rms() == 0.0

    plant.smu_output_on = True
    plant.smu_voltage_setpoint_v = 50.0
    plant.wavegen_c1_on = True
    low_bus_current = scope.rms_current()
    assert low_bus_current is not None
    assert low_bus_current > 0.0

    plant.smu_voltage_setpoint_v = 100.0
    assert scope.rms_current() > low_bus_current

    baseline_current = scope.rms_current()
    baseline_switch_current = scope.isw_rms()
    plant.wavegen_duty_pct = 25.0
    assert scope.rms_current() < baseline_current
    assert scope.isw_rms() < baseline_switch_current

    plant.wavegen_duty_pct = 50.0
    plant.wavegen_frequency_hz = 27_000_000.0
    assert scope.rms_current() < baseline_current

    plant.wavegen_frequency_hz = 13_000_000.0
    plant.wavegen_c2_on = True
    assert scope.rms_current() > baseline_current
    assert scope.isw_rms() != baseline_switch_current


def test_simulated_hdo4054_screenshot_is_watermarked_and_deterministic(
    tmp_path,
) -> None:
    plant = SimulatedRigPlant()
    plant.smu_output_on = True
    plant.smu_voltage_setpoint_v = 100.0
    plant.wavegen_c1_on = True
    client = MockScpiTcpClient("HDO4054", plant)
    scope = LeCroyHdo4054(client)
    first = tmp_path / "simulation-first.png"
    second = tmp_path / "simulation-second.png"

    assert scope.screenshot(first) == first
    assert scope.screenshot(second) == second

    payload = first.read_bytes()
    assert payload == second.read_bytes()
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", payload[16:24]) == (800, 450)
    assert b"SIMULATED DATA - NO HARDWARE CONNECTED" in payload

    plant.smu_voltage_setpoint_v = 80.0
    changed = tmp_path / "simulation-changed.png"
    assert scope.screenshot(changed) == changed
    assert changed.read_bytes() != payload


def test_simulated_scope_without_renderer_never_uses_adapter(tmp_path) -> None:
    session = RecordingSession()
    adapter = RecordingScreenshotAdapter(_valid_png())
    scope = LeCroyHdo4054(session, powi_scope=adapter)
    destination = tmp_path / "missing-renderer.png"

    assert scope.screenshot(destination) is None
    assert adapter.destination is None
    assert not destination.exists()


def test_driver_constructs_from_a_bare_session() -> None:
    scope = LeCroyHdo4054(RecordingSession())

    assert isinstance(scope, LeCroyHdo4054)


def test_hdo4054_screenshot_uses_exact_configured_visa_session(tmp_path) -> None:
    png = _valid_png()
    length = str(len(png)).encode()
    block = b"#" + str(len(length)).encode() + length + png + b"\r\n"
    resource = FakeScopeVisaResource(block)
    client, transport = _configured_scope_client(resource)
    scope = LeCroyHdo4054(client)
    destination = tmp_path / "hdo4054.png"

    assert scope.screenshot(destination) == destination

    assert destination.read_bytes() == png
    assert transport.resource == "TCPIP0::scope.example::inst0::INSTR"
    assert resource.operations == [
        ("write", "HCSU DEV,PNG,AREA,GRIDAREAONLY,DEST,REMOTE"),
        ("write", "SCDP"),
        ("read-bytes", 32 * 1024 * 1024 + 1),
    ]
    assert resource.binary_session_state == [(15000, None)]
    assert resource.timeout == 5000
    assert resource.read_termination == "\n"


def test_corrupt_hdo4054_png_preserves_existing_destination(tmp_path) -> None:
    corrupt_png = bytearray(_valid_png())
    corrupt_png[-1] ^= 0xFF
    resource = FakeScopeVisaResource(bytes(corrupt_png))
    client, _transport = _configured_scope_client(resource)
    scope = LeCroyHdo4054(client)
    destination = tmp_path / "existing.png"
    destination.write_bytes(b"existing capture")

    assert scope.screenshot(destination) is None

    assert destination.read_bytes() == b"existing capture"
    assert not list(tmp_path.glob(".*.tmp"))


def test_binary_block_with_junk_after_payload_is_not_published(tmp_path) -> None:
    png = _valid_png()
    length = str(len(png)).encode()
    block = b"#" + str(len(length)).encode() + length + png + b"junk"
    resource = FakeScopeVisaResource(block)
    client, _transport = _configured_scope_client(resource)
    scope = LeCroyHdo4054(client)
    destination = tmp_path / "existing.png"
    destination.write_bytes(b"existing capture")

    assert scope.screenshot(destination) is None

    assert destination.read_bytes() == b"existing capture"
    assert not list(tmp_path.glob(".*.tmp"))


def test_injected_screenshot_adapter_is_validated_and_written_atomically(
    tmp_path,
) -> None:
    session = RecordingSession()
    session.is_simulated = False
    adapter = RecordingScreenshotAdapter(_valid_png())
    scope = LeCroyHdo4054(session, powi_scope=adapter)
    destination = tmp_path / "adapter.png"

    assert scope.screenshot(destination) == destination

    assert destination.read_bytes() == _valid_png()
    assert adapter.destination != str(destination)
    assert not list(tmp_path.glob(".*.png"))


def test_invalid_adapter_capture_preserves_existing_destination(tmp_path) -> None:
    session = RecordingSession()
    session.is_simulated = False
    adapter = RecordingScreenshotAdapter(b"partial capture")
    scope = LeCroyHdo4054(session, powi_scope=adapter)
    destination = tmp_path / "adapter.png"
    destination.write_bytes(b"existing capture")

    assert scope.screenshot(destination) is None

    assert destination.read_bytes() == b"existing capture"
    assert not list(tmp_path.glob(".*.png"))


def test_non_visa_session_does_not_open_a_second_protocol_connection(
    tmp_path,
) -> None:
    session = RecordingSession()
    session.is_simulated = False
    scope = LeCroyHdo4054(session)
    destination = tmp_path / "no-session-switch.png"

    assert scope.screenshot(destination) is None
    assert not destination.exists()
    assert session.queries == []
