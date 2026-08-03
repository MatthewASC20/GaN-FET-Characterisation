"""Transport/framing regression tests that do not require bench hardware."""

from __future__ import annotations

import threading
from collections import deque

import pytest

from gan_fet.core.diagnostics import run_hardware_diagnostics
from gan_fet.core.events import InstrumentCommandEvent, bus
from gan_fet.instruments.mock_scpi import MockScpiTcpClient, SimulatedRigPlant
from gan_fet.instruments.scpi import ScpiTcpClient
from gan_fet.instruments.smu import Keithley2400
from gan_fet.scpi import ScpiClient
from gan_fet.settings import InstrumentAddress, Settings, SmuSettings
from gan_fet.transport import (
    AddressError,
    PrologixFraming,
    Transport,
    VisaTransport,
    parse_address,
    parse_uri,
    transport_for_spec,
)
from gan_fet.transport.base import BinaryReadUnsupported, TransportError


class RecordingTransport(Transport):
    """Small transport double that exposes every physical line sent."""

    def __init__(self, responses: tuple[str, ...] = ()):
        self.writes: list[str] = []
        self.responses = deque(responses)
        self.open_count = 0
        self._open = False

    @property
    def description(self) -> str:
        return "recording transport"

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        if not self._open:
            self._open = True
            self.open_count += 1

    def close(self) -> None:
        self._open = False

    def write_line(self, command: str) -> None:
        self.writes.append(command)

    def read_line(self) -> str:
        return self.responses.popleft()


class FakeVisaResource:
    """Message-based VISA resource double with visible session state."""

    def __init__(
        self,
        binary: bytes = b"",
        *,
        read_error: Exception | None = None,
        read_started: threading.Event | None = None,
        read_release: threading.Event | None = None,
    ) -> None:
        self.binary = binary
        self.read_error = read_error
        self.read_started = read_started
        self.read_release = read_release
        self.timeout = 5000
        self.read_termination: str | None = "\n"
        self.operations: list[tuple[str, object]] = []
        self.read_session_state: list[tuple[int, str | None]] = []
        self.closed = False

    def write(self, command: str) -> int:
        self.operations.append(("write", command))
        return len(command)

    def query(self, command: str) -> str:
        self.operations.append(("query", command))
        return "1"

    def read(self) -> str:
        self.operations.append(("read-line", None))
        return "1"

    def read_bytes(
        self,
        count: int,
        *,
        break_on_termchar: bool,
    ) -> bytes:
        self.operations.append(("read-bytes", count))
        self.read_session_state.append((self.timeout, self.read_termination))
        assert break_on_termchar
        if self.read_started is not None:
            self.read_started.set()
        if self.read_release is not None:
            assert self.read_release.wait(timeout=2.0)
        if self.read_error is not None:
            raise self.read_error
        return self.binary[:count]

    def close(self) -> None:
        self.closed = True


def _visa_client(
    resource: FakeVisaResource,
    address: str = "TCPIP0::scope.example::inst0::INSTR",
) -> tuple[ScpiClient, VisaTransport]:
    transport = VisaTransport(address)
    transport._visa = resource
    return ScpiClient("scope", transport), transport


@pytest.mark.parametrize(
    ("address", "port", "kind", "target"),
    [
        ("127.0.0.1", 5025, "tcp", "127.0.0.1"),
        ("COM3", 19200, "serial", "COM3"),
        ("/dev/ttyUSB0", 9600, "serial", "/dev/ttyUSB0"),
        ("GPIB0::24::INSTR", 9600, "visa", "GPIB0::24::INSTR"),
    ],
)
def test_legacy_address_resolution(address, port, kind, target):
    spec = parse_address(address, port)
    assert spec.kind == kind
    assert spec.target == target


def test_explicit_prologix_uri_and_validation():
    spec = parse_uri(
        "prologix+serial://COM3?baud=19200&addr=24&auto=0&timeout=1.5"
    )
    assert spec.is_serial
    assert spec.baud == 19200
    assert spec.prologix_addr == 24
    assert spec.prologix_auto is False
    assert spec.timeout_s == pytest.approx(1.5)

    for malformed in (
        "tcp://localhost:not-a-port",
        "prologix+tcp://localhost:1234",
        "prologix+tcp://localhost:1234?addr=31",
        "bogus://instrument",
    ):
        with pytest.raises(AddressError):
            parse_uri(malformed)


def test_direct_visa_ignores_external_prologix_setting():
    spec = parse_address(
        "GPIB0::24::INSTR", 9600, prologix_addr=24
    )
    transport = transport_for_spec(spec)

    assert spec.prologix_addr is None
    assert isinstance(transport, VisaTransport)
    assert transport.is_visa
    assert not transport.handles_prologix_framing


def test_prologix_framing_precedes_scpi_and_handles_manual_reads():
    inner = RecordingTransport(("ACME,K2410,1,1.0",))
    framing = PrologixFraming(
        inner, 24, auto_read=False, read_timeout_ms=1500, settle_s=0
    )
    client = ScpiClient("K2410", framing)

    assert framing.inner is inner
    assert framing.configuration_commands == (
        "++mode 1",
        "++addr 24",
        "++auto 0",
        "++eoi 1",
        "++read_tmo_ms 1500",
    )
    assert client.query("*IDN?") == "ACME,K2410,1,1.0"
    assert inner.writes == [
        *framing.configuration_commands,
        "*IDN?",
        "++read eoi",
    ]
    assert inner.open_count == 1


def test_smu_never_sends_prologix_commands_to_direct_visa():
    client = ScpiTcpClient("K2410", "GPIB0::24::INSTR", 9600)
    recorder = RecordingTransport()
    client.transport = recorder
    settings = SmuSettings(prologix_gpib_addr=24)
    smu = Keithley2400(client, settings)

    assert smu.initialize()
    smu.go_local()

    assert recorder.writes[0] == "*RST"
    assert recorder.writes[-1] == ":SYST:LOC"
    assert all(not command.startswith("++") for command in recorder.writes)


def test_legacy_tcp_smu_is_wrapped_before_first_command():
    client = ScpiTcpClient("K2410", "127.0.0.1", 1234)
    Keithley2400(client, SmuSettings(prologix_gpib_addr=24))

    assert client.uses_prologix
    assert isinstance(client.transport, PrologixFraming)
    assert client.transport.gpib_addr == 24


def test_mock_k2410_reset_and_raw_line_query():
    plant = SimulatedRigPlant()
    plant.smu_output_on = True
    plant.smu_voltage_setpoint_v = 325.0
    client = MockScpiTcpClient("K2410", shared_plant=plant)

    assert client.handle_line("*RST") is None
    assert plant.smu_output_on is False
    assert plant.smu_voltage_setpoint_v == 0.0
    assert client.handle_line(
        "VBS? 'return=app.Measure.P1.Out.Result.Value'"
    ) == "0.000000"


def test_hdo4054_diagnostic_rejects_a_responsive_wrong_scope(
    monkeypatch: pytest.MonkeyPatch,
):
    queries: list[tuple[str, str]] = []

    class DiagnosticClient:
        def __init__(self, name, *_args, **_kwargs):
            self.name = name

        def connect(self):
            return True

        def query(self, command):
            queries.append((self.name, command))
            return "LECROY,HDO4054A,1001,1.0"

        def query_float(self, _command):
            return None

        def write(self, _command):
            return True

        def request_local_control(self):
            return True

        def close(self):
            return None

    monkeypatch.setattr(
        "gan_fet.instruments.scpi.ScpiTcpClient",
        DiagnosticClient,
    )
    settings = Settings(
        instruments={
            "HDO4054": InstrumentAddress(
                "TCPIP0::scope.example::inst0::INSTR",
                0,
            )
        }
    )

    report = run_hardware_diagnostics(settings)

    assert not report.all_passed
    assert len(report.results) == 1
    assert not report.results[0].online
    assert "Expected LECROY HDO4054" in (report.results[0].error_msg or "")
    assert queries == [("HDO4054", "*IDN?")]


def test_hdo4054_diagnostic_reuses_strict_identity_and_keeps_others_generic(
    monkeypatch: pytest.MonkeyPatch,
):
    identities = {
        "HDO4054": "*IDN LECROY,HDO4054,HDO000001,8.3.0",
        "SDG6022X": "SIGLENT,SDG6022X,1002,1.0",
    }

    class DiagnosticClient:
        def __init__(self, name, *_args, **_kwargs):
            self.name = name

        def connect(self):
            return True

        def query(self, _command):
            return identities[self.name]

        def query_float(self, _command):
            return None

        def write(self, _command):
            return True

        def request_local_control(self):
            return True

        def close(self):
            return None

    monkeypatch.setattr(
        "gan_fet.instruments.scpi.ScpiTcpClient",
        DiagnosticClient,
    )
    settings = Settings(
        instruments={
            "HDO4054": InstrumentAddress(
                "TCPIP0::scope.example::inst0::INSTR",
                0,
            ),
            "SDG6022X": InstrumentAddress("wavegen.example", 5025),
        }
    )

    report = run_hardware_diagnostics(settings)

    assert report.all_passed
    assert [result.idn_string for result in report.results] == [
        identities["HDO4054"],
        identities["SDG6022X"],
    ]


def test_malformed_addresses_are_individual_diagnostic_failures():
    settings = Settings(
        instruments={
            "bad-port": InstrumentAddress("tcp://localhost:not-a-port", 1),
            "bad-scheme": InstrumentAddress("bogus://instrument", 1),
        }
    )

    report = run_hardware_diagnostics(settings, timeout_s=0.01)

    assert [result.instrument_name for result in report.results] == [
        "bad-port",
        "bad-scheme",
    ]
    assert all(not result.online for result in report.results)
    assert all(result.error_msg for result in report.results)


def test_binary_query_is_bounded_restores_visa_state_and_redacts_event():
    resource = FakeVisaResource(b"PNG")
    client, transport = _visa_client(resource)
    events: list[InstrumentCommandEvent] = []
    unsubscribe = bus.subscribe(InstrumentCommandEvent, events.append)
    try:
        response = client.query_bytes(
            "SCDP",
            max_bytes=8,
            timeout_s=0.25,
        )
    finally:
        unsubscribe()

    assert response == b"PNG"
    assert transport.is_open
    assert resource.operations == [
        ("write", "SCDP"),
        ("read-bytes", 9),
    ]
    assert resource.read_session_state == [(250, None)]
    assert resource.timeout == 5000
    assert resource.read_termination == "\n"
    assert len(events) == 1
    assert events[0].response == "<binary 3 bytes>"
    assert "PNG" not in (events[0].response or "")


def test_binary_query_rejects_unsupported_stream_before_writing():
    recorder = RecordingTransport()
    client = ScpiClient("raw-socket", recorder)

    with pytest.raises(BinaryReadUnsupported, match="does not support"):
        client.query_bytes("SCDP", max_bytes=1024, timeout_s=1.0)

    assert recorder.writes == []
    assert not recorder.is_open


@pytest.mark.parametrize(
    "address",
    (
        "TCPIP0::scope.example::5025::SOCKET",
        "ASRL3::INSTR",
    ),
)
def test_unframed_visa_binary_query_is_explicitly_unsupported(address):
    resource = FakeVisaResource(b"PNG")
    client, _transport = _visa_client(resource, address)

    with pytest.raises(BinaryReadUnsupported, match="does not support"):
        client.query_bytes("SCDP", max_bytes=1024, timeout_s=1.0)

    assert resource.operations == []


@pytest.mark.parametrize(
    "resource",
    (
        FakeVisaResource(b"12345"),
        FakeVisaResource(read_error=RuntimeError("lost EOI")),
    ),
)
def test_binary_failure_restores_visa_state_and_closes_session(resource):
    client, transport = _visa_client(resource)

    with pytest.raises(TransportError):
        client.query_bytes("SCDP", max_bytes=4, timeout_s=0.5)

    assert resource.read_session_state == [(500, None)]
    assert resource.timeout == 5000
    assert resource.read_termination == "\n"
    assert resource.closed
    assert not transport.is_open


def test_transaction_prevents_interleaving_during_binary_transfer():
    read_started = threading.Event()
    read_release = threading.Event()
    interloper_done = threading.Event()
    resource = FakeVisaResource(
        b"image",
        read_started=read_started,
        read_release=read_release,
    )
    client, _transport = _visa_client(resource)
    failures: list[BaseException] = []

    def capture() -> None:
        try:
            with client.transaction():
                assert client.write("CAPTURE:SETUP")
                assert client.query_bytes(
                    "CAPTURE:DATA?",
                    max_bytes=16,
                    timeout_s=1.0,
                ) == b"image"
                assert client.write("CAPTURE:CLEANUP")
        except BaseException as exc:
            failures.append(exc)

    def interloper() -> None:
        try:
            assert client.write("MEASURE?")
        except BaseException as exc:
            failures.append(exc)
        finally:
            interloper_done.set()

    capture_thread = threading.Thread(target=capture)
    capture_thread.start()
    assert read_started.wait(timeout=1.0)
    interloper_thread = threading.Thread(target=interloper)
    interloper_thread.start()
    assert not interloper_done.wait(timeout=0.05)

    read_release.set()
    capture_thread.join(timeout=2.0)
    interloper_thread.join(timeout=2.0)

    assert not failures
    assert not capture_thread.is_alive()
    assert not interloper_thread.is_alive()
    assert resource.operations == [
        ("write", "CAPTURE:SETUP"),
        ("write", "CAPTURE:DATA?"),
        ("read-bytes", 17),
        ("write", "CAPTURE:CLEANUP"),
        ("write", "MEASURE?"),
    ]
