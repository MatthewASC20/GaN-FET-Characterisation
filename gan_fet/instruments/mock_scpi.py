"""Shared software-in-the-loop SCPI transport.

Every simulated instrument client can point at one :class:`SimulatedRigPlant`.
This matters because scope and DMM readings must respond to the voltage sourced
by the simulated SMU and to the gate state of the simulated wave generator.
"""

from __future__ import annotations

import logging
import math
import re
import struct
import threading
import time
import zlib
from typing import Optional

from gan_fet.core.command_logger import classify_scpi_risk
from gan_fet.core.events import InstrumentCommandEvent, bus

log = logging.getLogger(__name__)


class SimulatedRigPlant:
    """Deterministic shared state for an entire simulated GaN test rig."""

    #: Nominal matrix frequencies the simulated tank can be built for.
    NOMINAL_BANDS_HZ = (6_000_000.0, 13_000_000.0, 27_000_000.0)

    def __init__(
        self,
        *,
        peak_gain: float = 3.0,
        zvs_voltage_v: float = 95.0,
        resonant_model: bool = False,
        resonance_offset_frac: float = 0.065,
        tank_q: float = 8.0,
        resonant_peak_gain: float = 4.2,
        coss_shift_frac: float = 0.045,
        zvs_onset_gain: float = 3.2,
        off_resonance_gain: float = 1.15,
    ):
        self.lock = threading.RLock()
        self.peak_gain = float(peak_gain)
        self.zvs_voltage_v = float(zvs_voltage_v)

        # -- optional resonant pathology ---------------------------------
        # Off by default so the historical smooth plant is unchanged.  When
        # enabled the tank gains a frequency response, an amplitude-dependent
        # resonance shift, and two competing loss terms.  This is *not* a
        # circuit model; it exists so frequency-search code paths, their
        # interlocks and their minimum-selection logic can be exercised
        # without bench hardware.
        self.resonant_model = bool(resonant_model)
        self.resonance_offset_frac = float(resonance_offset_frac)
        self.tank_q = float(tank_q)
        self.resonant_peak_gain = float(resonant_peak_gain)
        self.coss_shift_frac = float(coss_shift_frac)
        # Tank gain at which the drain first reaches zero before turn-on.
        # Below it the dwell is exactly zero, which is what makes ZVS onset
        # a crossing rather than a minimum.
        self.zvs_onset_gain = float(zvs_onset_gain)
        # Gain far from resonance. Never below unity: with the switch off the
        # choke holds current and the drain flies up to at least the bus, so
        # the resonant boost adds to that floor rather than replacing it. A
        # bare Lorentzian decays to zero instead, which invents a regime where
        # the bus must exceed the peak it is producing.
        self.off_resonance_gain = float(off_resonance_gain)
        # Latched when the gates are armed: a physical bank does not retune
        # itself mid-run, so a frequency sweep must see a fixed resonance.
        self.tank_nominal_hz: Optional[float] = None

        self.smu_output_on = False
        self.smu_voltage_setpoint_v = 0.0
        self.smu_current_limit_a = 0.1
        self.force_compliance = False

        self.wavegen_frequency_hz = 13_000_000.0
        self.wavegen_duty_pct = 50.0
        self.wavegen_c1_on = False
        self.wavegen_c2_on = False
        self.wavegen_coupled = False
        #: Last ZVS threshold written by the software, for assertions.
        self.zvs_threshold_v: Optional[float] = None

    @property
    def bus_voltage_v(self) -> float:
        return self.smu_voltage_setpoint_v if self.smu_output_on else 0.0

    @property
    def gates_armed(self) -> bool:
        return self.wavegen_c1_on

    # -- resonant tank model (only when ``resonant_model`` is set) ---------

    def latch_tank_nominal(self) -> None:
        """Fix the simulated bank to the nearest nominal band.

        Called when the gates are armed. A real inductor and capacitor bank is
        built for one nominal frequency and does not change during a sweep, so
        the resonance must not follow the wavegen around the window.
        """
        if not self.resonant_model:
            return
        frequency = max(1.0, float(self.wavegen_frequency_hz))
        self.tank_nominal_hz = min(
            self.NOMINAL_BANDS_HZ, key=lambda band: abs(band - frequency)
        )

    def _small_signal_resonance_hz(self) -> float:
        nominal = self.tank_nominal_hz
        if nominal is None:
            nominal = min(
                self.NOMINAL_BANDS_HZ,
                key=lambda band: abs(band - max(1.0, self.wavegen_frequency_hz)),
            )
        # The built bank never lands exactly on nominal — that is the whole
        # reason a frequency search exists.
        return nominal * (1.0 + self.resonance_offset_frac)

    def _tank_state(self) -> tuple[float, float]:
        """Return ``(gain, detuning)`` for the present operating point.

        Coss falls as Vds rises, so the resonance moves *up* with amplitude.
        Amplitude in turn depends on gain, so the two are solved by a short
        fixed-point iteration — that coupling is the nonlinearity being
        simulated.
        """
        bus_voltage = max(0.0, self.bus_voltage_v)
        frequency = max(1.0, float(self.wavegen_frequency_hz))
        f_small = self._small_signal_resonance_hz()

        floor = self.off_resonance_gain
        boost = max(0.0, self.resonant_peak_gain - floor)
        gain = self.resonant_peak_gain
        detuning = 0.0
        for _ in range(6):
            peak = gain * bus_voltage
            f_res = f_small * (1.0 + self.coss_shift_frac * min(1.0, peak / 400.0))
            detuning = (frequency - f_res) / f_res
            # The resonant boost decays with detuning; the floor does not.
            gain = floor + boost / math.sqrt(
                1.0 + (2.0 * self.tank_q * detuning) ** 2
            )
        return gain, detuning

    def _resonant_loss_a(self) -> float:
        """Two competing loss terms, giving two minima either side of resonance.

        Circulating current peaks sharply at resonance, so conduction loss has
        a local *maximum* there. Switching loss falls broadly as resonance is
        approached. Their sum therefore dips on both shoulders, which is the
        multi-minimum structure the search must cope with. A slight tilt makes
        the two minima unequal so there is a well-defined global answer.
        """
        _gain, detuning = self._tank_state()
        lorentz = 1.0 / (1.0 + (2.0 * self.tank_q * detuning) ** 2)
        circulating = 0.035 * lorentz**2
        switching = 0.010 * (1.0 - 0.86 * lorentz) * (1.0 + 3.5 * detuning)
        return circulating + max(0.0, switching)

    def dc_current_a(self) -> float:
        if not self.smu_output_on:
            return 0.0
        # Smooth rounded-V curve with a minimum at the simulated ZVS point. One
        # default 1 V search step must exceed the tuner's minimum-improvement
        # threshold, while the normal envelope remains below 100 mA.
        delta_v = self.bus_voltage_v - self.zvs_voltage_v
        current = 0.020 + 2e-4 * (math.sqrt(delta_v**2 + 1.0) - 1.0)
        if self.resonant_model and self.gates_armed:
            current += self._resonant_loss_a()
        return current

    def vds_peak_v(self) -> float:
        if not self.gates_armed:
            return 0.0
        if not self.resonant_model:
            return self.peak_gain * self.bus_voltage_v
        gain, _detuning = self._tank_state()
        return gain * self.bus_voltage_v

    def rms_current_a(self) -> float:
        """Return a plausible deterministic P2 reading for the virtual rig.

        The model is deliberately simple rather than an electrical design
        model.  It preserves the relationships an operator needs to exercise
        the acquisition workflow: current is zero with either source off, and
        otherwise responds smoothly to bus voltage, duty cycle, frequency,
        and dual-channel operation.
        """
        if not self.smu_output_on or not self.gates_armed:
            return 0.0

        bus_voltage = max(0.0, self.bus_voltage_v)
        if bus_voltage == 0.0:
            return 0.0

        duty_pct = min(95.0, max(5.0, self.wavegen_duty_pct))
        frequency_hz = max(100_000.0, self.wavegen_frequency_hz)
        duty_factor = (duty_pct / 50.0) ** 0.55
        frequency_factor = (13_000_000.0 / frequency_hz) ** 0.12
        frequency_factor = min(1.35, max(0.75, frequency_factor))
        channel_factor = 1.12 if self.wavegen_c2_on else 1.0

        # At the default simulated operating point (100 V, 13 MHz, 50%)
        # this remains 2.5 A, matching the historical demonstration value.
        voltage_response = 0.05 + 0.0245 * bus_voltage
        return voltage_response * duty_factor * frequency_factor * channel_factor

    def switch_current_rms_a(self) -> float:
        """Return a deterministic per-switch P3 reading for the virtual rig."""
        load_rms = self.rms_current_a()
        if load_rms == 0.0:
            return 0.0
        duty_pct = min(95.0, max(5.0, self.wavegen_duty_pct))
        conduction_factor = 0.32 + 0.0032 * duty_pct
        channel_sharing = 0.78 if self.wavegen_c2_on else 1.0
        return load_rms * conduction_factor * channel_sharing

    def zvs_dwell_fraction(self) -> float:
        """Fraction of the cycle Vds sits below the ZVS threshold.

        Modelled from tank gain rather than from a waveform: below the ZVS
        condition the tank cannot pull the drain to zero and the dwell is
        exactly zero; past it the body diode holds the drain down and the
        dwell widens with how hard the tank is driven.

        The zero region is the point of the model. It is what makes onset a
        threshold crossing rather than an extremum, and therefore what any
        bisection search would rely on.
        """
        if not self.resonant_model or not self.gates_armed or not self.smu_output_on:
            return 0.0
        gain, _detuning = self._tank_state()
        # Below this gain the tank cannot complete the transition in the
        # available dead time, so Vds never reaches the threshold.
        onset = self.zvs_onset_gain
        if gain <= onset:
            return 0.0
        excess = (gain - onset) / max(1e-6, onset)
        # Saturating: diode conduction cannot occupy the whole period.
        return min(0.45, 0.55 * excess)

    def compliance_tripped(self) -> bool:
        return self.force_compliance or (
            self.smu_output_on
            and abs(self.dc_current_a()) >= self.smu_current_limit_a
        )


_FONT_5X7: dict[str, tuple[str, ...]] = {
    " ": ("00000",) * 7,
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    ":": ("00000", "01100", "01100", "00000", "01100", "01100", "00000"),
    "%": ("11001", "11010", "00100", "01000", "10110", "00110", "00000"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "10010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
}


class _RgbCanvas:
    """Tiny dependency-free RGB raster used for virtual scope captures."""

    def __init__(self, width: int, height: int, color: tuple[int, int, int]):
        self.width = width
        self.height = height
        self.pixels = bytearray(color * (width * height))

    def pixel(self, x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            offset = (y * self.width + x) * 3
            self.pixels[offset : offset + 3] = bytes(color)

    def rectangle(
        self,
        left: int,
        top: int,
        right: int,
        bottom: int,
        color: tuple[int, int, int],
    ) -> None:
        left = max(0, left)
        top = max(0, top)
        right = min(self.width, right)
        bottom = min(self.height, bottom)
        row = bytes(color) * max(0, right - left)
        for y in range(top, bottom):
            offset = (y * self.width + left) * 3
            self.pixels[offset : offset + len(row)] = row

    def line(
        self,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        color: tuple[int, int, int],
        *,
        width: int = 1,
    ) -> None:
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        step_x = 1 if x0 < x1 else -1
        step_y = 1 if y0 < y1 else -1
        error = dx + dy
        radius = max(0, width // 2)
        while True:
            self.rectangle(
                x0 - radius,
                y0 - radius,
                x0 + radius + 1,
                y0 + radius + 1,
                color,
            )
            if x0 == x1 and y0 == y1:
                return
            doubled = 2 * error
            if doubled >= dy:
                error += dy
                x0 += step_x
            if doubled <= dx:
                error += dx
                y0 += step_y

    def text(
        self,
        x: int,
        y: int,
        value: str,
        color: tuple[int, int, int],
        *,
        scale: int = 1,
    ) -> None:
        cursor = x
        for char in value.upper():
            glyph = _FONT_5X7.get(char, _FONT_5X7[" "])
            for row_index, row in enumerate(glyph):
                for column_index, enabled in enumerate(row):
                    if enabled == "1":
                        self.rectangle(
                            cursor + column_index * scale,
                            y + row_index * scale,
                            cursor + (column_index + 1) * scale,
                            y + (row_index + 1) * scale,
                            color,
                        )
            cursor += 6 * scale

    def png(self, *, description: str) -> bytes:
        scanlines = b"".join(
            b"\x00"
            + bytes(self.pixels[y * self.width * 3 : (y + 1) * self.width * 3])
            for y in range(self.height)
        )
        ihdr = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"tEXt", b"Description\x00" + description.encode("ascii"))
            + _png_chunk(b"IDAT", zlib.compress(scanlines, level=9))
            + _png_chunk(b"IEND", b"")
        )


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def _render_simulated_scope_png(plant: SimulatedRigPlant) -> bytes:
    """Render a deterministic HDO4054-style display from one plant snapshot."""
    with plant.lock:
        bus_voltage = plant.bus_voltage_v
        vds_peak = plant.vds_peak_v()
        rms_current = plant.rms_current_a()
        switch_rms = plant.switch_current_rms_a()
        frequency_hz = plant.wavegen_frequency_hz
        duty_pct = plant.wavegen_duty_pct
        gates_armed = plant.gates_armed
        dual_channel = plant.wavegen_c2_on

    canvas = _RgbCanvas(800, 450, (8, 12, 20))
    canvas.rectangle(0, 0, 800, 32, (162, 20, 42))
    canvas.text(
        154,
        8,
        "SIMULATED DATA - NO HARDWARE CONNECTED",
        (255, 255, 255),
        scale=2,
    )
    canvas.text(18, 42, "HDO4054 VIRTUAL OSCILLOSCOPE", (214, 224, 239), scale=2)

    plot_left, plot_top, plot_right, plot_bottom = 20, 72, 610, 390
    canvas.rectangle(plot_left, plot_top, plot_right, plot_bottom, (5, 9, 16))
    for division in range(11):
        x = plot_left + division * (plot_right - plot_left) // 10
        canvas.line(x, plot_top, x, plot_bottom - 1, (35, 48, 64))
    for division in range(9):
        y = plot_top + division * (plot_bottom - plot_top) // 8
        canvas.line(plot_left, y, plot_right - 1, y, (35, 48, 64))

    # Fixed four-cycle view makes image generation independent of wall-clock
    # timing while the trace shapes and measurements still follow plant state.
    last_vds: Optional[tuple[int, int]] = None
    last_current: Optional[tuple[int, int]] = None
    last_switch: Optional[tuple[int, int]] = None
    duty_fraction = min(0.95, max(0.05, duty_pct / 100.0))
    trace_width = plot_right - plot_left
    for x_offset in range(trace_width):
        phase = 4.0 * x_offset / max(1, trace_width - 1)
        cycle_phase = phase % 1.0
        gate_high = gates_armed and cycle_phase < duty_fraction

        if not gates_armed or bus_voltage <= 0.0:
            vds_value = current_value = switch_value = 0.0
        else:
            off_phase = max(0.0, cycle_phase - duty_fraction)
            ring = (
                0.10
                * vds_peak
                * math.exp(-18.0 * off_phase)
                * math.sin(70.0 * off_phase)
                if not gate_high
                else 0.0
            )
            vds_value = (0.04 * vds_peak if gate_high else vds_peak) + ring
            current_value = rms_current * math.sqrt(2.0) * math.sin(2.0 * math.pi * phase)
            switch_value = current_value if gate_high else 0.0

        vds_scale = max(400.0, vds_peak * 1.15, 1.0)
        vds_y = int(222 - 125 * vds_value / vds_scale)
        current_scale = max(4.0, rms_current * 1.8, switch_rms * 2.0, 1.0)
        current_y = int(292 - 55 * current_value / current_scale)
        switch_y = int(350 - 42 * switch_value / current_scale)
        x = plot_left + x_offset

        # The three trace cursors are always advanced together, so one being
        # set implies all three are.  Bind them explicitly rather than starring
        # the tuples: it states that invariant instead of assuming it.
        if last_vds is not None and last_current is not None and last_switch is not None:
            canvas.line(last_vds[0], last_vds[1], x, vds_y, (255, 214, 51), width=2)
            canvas.line(
                last_current[0], last_current[1], x, current_y, (49, 214, 255), width=2
            )
            canvas.line(
                last_switch[0], last_switch[1], x, switch_y, (255, 77, 196), width=2
            )
        last_vds = (x, vds_y)
        last_current = (x, current_y)
        last_switch = (x, switch_y)

    # This central mark is deliberately drawn over the traces.  Exported
    # simulation captures cannot be mistaken for physical scope evidence.
    canvas.text(175, 198, "SIMULATION", (137, 43, 55), scale=5)

    canvas.rectangle(620, 72, 790, 390, (13, 20, 31))
    canvas.text(633, 86, "MEASUREMENTS", (220, 230, 242), scale=2)
    canvas.text(634, 119, "P1 VDS", (255, 214, 51), scale=2)
    canvas.text(634, 138, f"{vds_peak:7.1f} V", (255, 214, 51), scale=2)
    canvas.text(634, 174, "P2 IRMS", (49, 214, 255), scale=2)
    canvas.text(634, 193, f"{rms_current:7.3f} A", (49, 214, 255), scale=2)
    canvas.text(634, 229, "P3 ISW", (255, 77, 196), scale=2)
    canvas.text(634, 248, f"{switch_rms:7.3f} A", (255, 77, 196), scale=2)
    canvas.text(634, 284, f"BUS {bus_voltage:6.1f} V", (181, 193, 211))
    canvas.text(634, 301, f"FREQ {frequency_hz / 1e6:5.2f} MHZ", (181, 193, 211))
    canvas.text(634, 318, f"DUTY {duty_pct:5.1f}%", (181, 193, 211))
    canvas.text(634, 335, f"GATES {'ON' if gates_armed else 'OFF'}", (181, 193, 211))
    canvas.text(634, 352, f"MODE {'DUAL' if dual_channel else 'SINGLE'}", (181, 193, 211))

    canvas.rectangle(0, 406, 800, 450, (21, 30, 43))
    canvas.text(
        232,
        416,
        "SYNTHETIC WAVEFORMS",
        (255, 122, 139),
        scale=2,
    )
    canvas.text(
        226,
        437,
        "FOR PROCEDURE VALIDATION ONLY",
        (220, 230, 242),
    )
    return canvas.png(description="SIMULATED DATA - NO HARDWARE CONNECTED")


_default_plant = SimulatedRigPlant()


def reset_default_simulated_plant() -> SimulatedRigPlant:
    """Replace and return the process-default plant (primarily for tests)."""
    global _default_plant
    _default_plant = SimulatedRigPlant()
    return _default_plant


class MockScpiTcpClient:
    """Drop-in :class:`ScpiTcpClient` substitute backed by a shared plant."""

    is_simulated = True

    def __init__(
        self,
        name: str,
        shared_plant: Optional[SimulatedRigPlant] = None,
        host: str = "127.0.0.1",
        port: int = 5025,
        timeout: float = 1.0,
    ):
        self.name = name
        self.host = host
        self.port = port
        self.timeout = timeout
        self.shared_plant = shared_plant or _default_plant
        self._connected = True
        self._lock = threading.RLock()
        self._prologix_addr: Optional[int] = None

    @property
    def is_visa(self) -> bool:
        return False

    @property
    def is_serial(self) -> bool:
        return False

    @property
    def handles_prologix_framing(self) -> bool:
        return self._prologix_addr is not None

    def configure_prologix(self, gpib_addr: int, *, auto_read: bool = True) -> bool:
        """Mirror the production client's framing configuration API."""
        del auto_read  # The in-process plant has no byte-level read mode.
        if isinstance(gpib_addr, bool) or not isinstance(gpib_addr, int):
            raise ValueError(f"GPIB address {gpib_addr!r} is not an integer")
        if not 0 <= gpib_addr <= 30:
            raise ValueError(
                f"GPIB address {gpib_addr} is outside the valid range 0-30"
            )
        self._prologix_addr = int(gpib_addr)
        return True

    def connect(self) -> bool:
        with self._lock:
            self._connected = True
            return True

    def close(self) -> None:
        with self._lock:
            self._connected = False

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def write(self, command: str) -> bool:
        start_time = time.time()
        with self._lock:
            if not self.connect():
                return False
            clean_cmd = command.strip()
            self._process_write(clean_cmd)
            duration_ms = (time.time() - start_time) * 1000.0
            bus.publish(
                InstrumentCommandEvent(
                    timestamp=start_time,
                    instrument_name=self.name,
                    command=command,
                    response=None,
                    is_query=False,
                    risk_level=classify_scpi_risk(command),
                    duration_ms=duration_ms,
                    is_simulated=True,
                )
            )
            return True

    def query(self, command: str) -> Optional[str]:
        start_time = time.time()
        with self._lock:
            if not self.connect():
                return None
            clean_cmd = command.strip()
            response = self._process_query(clean_cmd)
            duration_ms = (time.time() - start_time) * 1000.0
            bus.publish(
                InstrumentCommandEvent(
                    timestamp=start_time,
                    instrument_name=self.name,
                    command=command,
                    response=response,
                    is_query=True,
                    risk_level=classify_scpi_risk(command),
                    duration_ms=duration_ms,
                    is_simulated=True,
                )
            )
            return response

    def query_float(self, command: str) -> Optional[float]:
        raw = self.query(command)
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            log.warning("%s: simulated non-numeric response %r", self.name, raw)
            return None

    def request_local_control(self) -> bool:
        command = "++loc" if self.handles_prologix_framing else ":SYST:LOC"
        return self.write(command)

    def simulated_screenshot_png(self) -> bytes:
        """Return a synthetic capture without opening any external transport."""
        return _render_simulated_scope_png(self.shared_plant)

    def handle_line(self, line: str) -> Optional[str]:
        """Process a raw command for socket-backed simulation front ends."""
        command = line.strip()
        if not command:
            return None
        with self._lock:
            # Some supported query forms include arguments after the ``?``;
            # checking containment handles them without a command registry.
            if "?" in command:
                return self._process_query(command)
            self._process_write(command)
            return None

    @staticmethod
    def _parse_number_after(command: str, marker: str) -> Optional[float]:
        match = re.search(
            rf"{re.escape(marker)}\s*,?\s*([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)",
            command,
            re.IGNORECASE,
        )
        if match is None:
            return None
        return float(match.group(1))

    def _process_write(self, command: str) -> None:
        upper = command.upper()
        plant = self.shared_plant
        with plant.lock:
            # Prologix framing and benign setup commands do not change the
            # simulated electrical plant.
            if upper.startswith("++"):
                return

            # The ZVS threshold is the one measurement setting the software
            # writes. Recorded rather than acted on: the simulated dwell is
            # derived from tank gain, not from thresholding a waveform.
            if "ABSLEVEL" in upper:
                # VBS assignment syntax (``AbsLevel = 10.0``) rather than the
                # comma-separated SCPI form the shared parser expects.
                match = re.search(
                    r"AbsLevel\s*=\s*([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)",
                    command,
                    re.IGNORECASE,
                )
                if match is not None:
                    plant.zvs_threshold_v = float(match.group(1))
                return

            normalized_name = self.name.upper()
            is_smu = (
                re.search(r"(?:K|KEITHLEY)24(?:00|10)", normalized_name)
                is not None
                or "SMU" in normalized_name
            )
            if upper == "*RST" and is_smu:
                plant.smu_output_on = False
                plant.smu_voltage_setpoint_v = 0.0
                return

            if re.search(r"(^|:)OUTP(?:UT)?\s+(?:ON|1)$", upper):
                if upper.startswith("C1:"):
                    plant.wavegen_c1_on = True
                    # The bank is fixed once the rig is energised; latch the
                    # simulated resonance so a sweep cannot drag it along.
                    plant.latch_tank_nominal()
                elif upper.startswith("C2:"):
                    plant.wavegen_c2_on = True
                else:
                    plant.smu_output_on = True
                return
            if re.search(r"(^|:)OUTP(?:UT)?\s+(?:OFF|0)$", upper):
                if upper.startswith("C1:"):
                    plant.wavegen_c1_on = False
                elif upper.startswith("C2:"):
                    plant.wavegen_c2_on = False
                else:
                    plant.smu_output_on = False
                return

            if upper in (":ABOR", "ABOR", ":ABORT", "ABORT"):
                plant.smu_output_on = False
                return

            if ":SOUR:VOLT " in upper or upper.startswith("SOUR:VOLT "):
                value = self._parse_number_after(command, "VOLT")
                if value is not None:
                    plant.smu_voltage_setpoint_v = value
                return
            if ":SENS:CURR:PROT " in upper:
                value = self._parse_number_after(command, "PROT")
                if value is not None:
                    plant.smu_current_limit_a = abs(value)
                return

            if upper.startswith("COUP STATE,"):
                plant.wavegen_coupled = upper.endswith("ON")
                return

            if upper.startswith("C1:BSWV") or upper.startswith("C2:BSWV"):
                frequency = self._parse_number_after(command, "FRQ")
                duty = self._parse_number_after(command, "DUTY")
                if frequency is not None:
                    plant.wavegen_frequency_hz = frequency
                if duty is not None:
                    plant.wavegen_duty_pct = duty

    def _process_query(self, command: str) -> Optional[str]:
        upper = command.upper()
        plant = self.shared_plant
        with plant.lock:
            if "*IDN?" in upper:
                if self.name.strip().upper() == "HDO4054":
                    return "*IDN LECROY,HDO4054,HDO4054SIM,1.0"
                return f"OpenAI-Labs,{self.name}-SIM,1001,1.0"

            if upper == ":READ?" or upper == "READ?":
                return f"{plant.bus_voltage_v:.6f},{plant.dc_current_a():.9f}"
            if upper in {":OUTP?", "OUTP?", ":OUTPUT?", "OUTPUT?"}:
                return "1" if plant.smu_output_on else "0"
            if "SENS:CURR:PROT:TRIP?" in upper:
                return "1" if plant.compliance_tripped() else "0"
            if "SOUR:VOLT?" in upper:
                return f"{plant.smu_voltage_setpoint_v:.6f}"

            if "MEAS:VOLT:DC?" in upper:
                return f"{plant.bus_voltage_v:.6f}"
            if upper == "MEAS:VOLT?":
                return f"{plant.bus_voltage_v:.3f}"
            if "MEAS:CURR?" in upper:
                return f"{plant.dc_current_a():.9f}"

            # HDO4054 MAUI measurement slots configured by the bench setup:
            # P1 = Vds peak, P2 = RMS current, P3 = switch-current RMS,
            # P4 = ZVS dwell. P4 is matched first: a "P4" query also contains
            # no other slot name, but keeping the order explicit avoids any
            # future substring collision.
            if "P4" in upper:
                # Reported as a percentage, matching MAUI's duty convention.
                return f"{plant.zvs_dwell_fraction() * 100.0:.6f}"
            if "P1" in upper:
                return f"{plant.vds_peak_v():.6f}"
            if "P2" in upper:
                return f"{plant.rms_current_a():.6f}"
            if "P3" in upper:
                return f"{plant.switch_current_rms_a():.6f}"

            if upper == "C1:OUTP?":
                return f"C1:OUTP {'ON' if plant.wavegen_c1_on else 'OFF'}"
            if upper == "C2:OUTP?":
                return f"C2:OUTP {'ON' if plant.wavegen_c2_on else 'OFF'}"
            if upper == "C1:BSWV?":
                return (
                    "C1:BSWV WVTP,SQUARE,"
                    f"FRQ,{plant.wavegen_frequency_hz:.0f}HZ,"
                    "AMP,6.5V,OFST,1.9V,"
                    f"DUTY,{plant.wavegen_duty_pct:.3f},PHSE,0"
                )

            if "SYST:ERR?" in upper:
                return '0,"No error"'
            if upper == "*OPC?":
                return "1"
            return None
