"""Simulated rig for the test suite.

Spins up fake TCP instruments (SMU, wavegen, scope, DMM) per test and wires
real drivers, safety monitor and database to them. The fake SMU implements a
parabolic I(V) plant with a minimum at `v_zvs`, and the fake scope reports
Vds peak = `peak_gain` * bus volts, so the closed-loop controllers can be
exercised for real.
"""

from __future__ import annotations

import socketserver
import threading
from types import SimpleNamespace

import pytest

from gan_fet.instruments.multimeter import Sdm3055
from gan_fet.instruments.oscilloscope import Mso44
from gan_fet.instruments.scpi import ScpiTcpClient
from gan_fet.instruments.smu import Keithley2400
from gan_fet.instruments.wavegen import Sdg6022x
from gan_fet.core.safety import SafetyMonitor
from gan_fet.settings import (
    PeakControlSettings,
    SafetySettings,
    Settings,
    SmuSettings,
    ZvsSettings,
)
from gan_fet.storage.db import Database


class RigState:
    def __init__(self):
        self.lock = threading.Lock()
        self.smu_volts = 0.0
        self.smu_output = False
        self.compliance = False
        self.v_zvs = 95.0
        self.i_floor = 0.020
        self.peak_gain = 3.0
        self.wavegen_freq = 6_000_000
        self.commands = {"smu": [], "wavegen": [], "scope": [], "dmm": []}

    def dc_current(self):
        return self.i_floor + 1e-5 * (self.smu_volts - self.v_zvs) ** 2

    def vds_peak(self):
        return self.peak_gain * self.smu_volts


def _make_handler(state: RigState, name: str, respond):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            while True:
                line = self.rfile.readline()
                if not line:
                    return
                cmd = line.decode().strip()
                with state.lock:
                    state.commands[name].append(cmd)
                    reply = respond(cmd)
                if reply is not None:
                    self.wfile.write((reply + "\n").encode())

    return Handler


def _smu_respond(state: RigState):
    def respond(cmd):
        if cmd.startswith(":SOUR:VOLT "):
            state.smu_volts = float(cmd.split()[-1])
        elif cmd == ":OUTP ON":
            state.smu_output = True
        elif cmd == ":OUTP OFF":
            state.smu_output = False
        elif cmd == ":READ?":
            return f"{state.smu_volts:.4f},{state.dc_current():.6e}"
        elif cmd == ":SENS:CURR:PROT:TRIP?":
            return "1" if state.compliance else "0"
        return None

    return respond


def _wavegen_respond(state: RigState):
    def respond(cmd):
        if cmd == "C1:BSWV?":
            return (
                f"C1:BSWV WVTP,SQUARE,FRQ,{state.wavegen_freq}HZ,"
                "AMP,6.5V,OFST,1.9V,DUTY,25,PHSE,0"
            )
        if cmd.startswith("C1:BSWV FRQ,"):
            state.wavegen_freq = int(float(cmd.split(",")[-1]))
        return None

    return respond


def _scope_respond(state: RigState):
    def respond(cmd):
        if cmd == "MEASUrement:MEAS6:VALue?":
            return f"{state.vds_peak():.4f}"
        if cmd == "MEASUrement:MEAS7:VALue?":
            return "2.5"
        if cmd == "MEASUrement:MEAS9:VALue?":
            return "1.2"
        return None

    return respond


def _dmm_respond(state: RigState):
    def respond(cmd):
        if cmd == "MEAS:VOLT:DC?":
            return f"{state.smu_volts:.4f}"
        return None

    return respond


@pytest.fixture
def rig(tmp_path):
    state = RigState()
    servers = []

    def start(name, respond):
        server = socketserver.ThreadingTCPServer(
            ("127.0.0.1", 0), _make_handler(state, name, respond)
        )
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]

    smu_port = start("smu", _smu_respond(state))
    wg_port = start("wavegen", _wavegen_respond(state))
    scope_port = start("scope", _scope_respond(state))
    dmm_port = start("dmm", _dmm_respond(state))

    settings = Settings()
    settings.data_dir = str(tmp_path)
    settings.smu = SmuSettings(
        prologix_gpib_addr=24, max_voltage_v=1100.0, current_compliance_a=0.1,
        nplc=1.0, sample_interval_s=0.05, ramp_step_v=50.0, ramp_delay_s=0.01,
    )
    settings.zvs = ZvsSettings(
        step_v=1.0, settle_s=0.01, samples_per_point=1,
        min_improvement_a=1e-6, window_v=50.0, max_steps=200,
    )
    settings.peak_control = PeakControlSettings(
        tolerance_v=2.0, settle_s=0.01, max_iterations=200,
        min_step_v=0.5, proportional_gain=0.4,
    )
    settings.safety = SafetySettings(
        max_dc_current_a=1.0, max_vds_peak_v=450.0,
        watchdog_consecutive_failures=5,
    )

    smu = Keithley2400(ScpiTcpClient("K2400", "127.0.0.1", smu_port), settings.smu)
    wavegen = Sdg6022x(ScpiTcpClient("SDG6022X", "127.0.0.1", wg_port))
    scope = Mso44(ScpiTcpClient("MSO44", "127.0.0.1", scope_port))
    dmm = Sdm3055(ScpiTcpClient("SDM3055", "127.0.0.1", dmm_port))
    scope.screenshot = lambda dest: None  # the fake rig has no pyvisa endpoint

    db = Database(settings.db_path)
    safety = SafetyMonitor(settings.safety, smu, wavegen, db)

    yield SimpleNamespace(
        state=state, settings=settings, smu=smu, wavegen=wavegen,
        scope=scope, dmm=dmm, db=db, safety=safety,
    )

    db.close()
    for client in (smu.client, wavegen.client, scope.client, dmm.client):
        client.close()
    for server in servers:
        server.shutdown()
        server.server_close()
