"""Siglent SDG6022X waveform generator — gates the FET(s).

Channel 1 drives the (first) device; channel 2 mirrors it in Dual
Conduction mode. Configuration leaves outputs OFF (the operator enables
them once the rig is ready), matching the original workflow.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Callable

from gan_fet.instruments.base import WavegenInterface
from gan_fet.scpi.protocol import ScpiSession

log = logging.getLogger(__name__)

# Gate-drive waveform fixed by the rig: square, 6.5 Vpp, +1.9 V offset.
_BSWV = "BSWV WVTP,SQUARE,FRQ,{freq},AMP,6.5VPP,OFST,1.9V,DUTY,{duty},PHSE,0"

DEFAULT_FREQ_RATE_KHZ_S = 200.0   # 200 kHz / sec
DEFAULT_DUTY_RATE_PCT_S = 10.0     # 10 % / sec


class Sdg6022x(WavegenInterface):
    def __init__(self, client: ScpiSession):
        self.client = client
        self._state_lock = threading.Lock()
        self._current_freq_hz: Optional[float] = None
        self._current_duty_pct: Optional[float] = None
        self._outputs_armed = False
        self._armed_config: Optional[str] = None
        self._configured_config: Optional[str] = None
        # Incremented whenever outputs are disarmed.  Long-running ramps take a
        # snapshot and stop if an emergency shutdown changes the epoch.
        self._output_epoch = 0

    @staticmethod
    def is_dual(config: str) -> bool:
        return config == "Dual Conduction"

    @staticmethod
    def _validate_config(config: str) -> None:
        if config not in ("Dual Conduction", "Single Conduction", "Single Device"):
            raise ValueError(f"Unknown configuration: {config}")

    @staticmethod
    def _validate_frequency(freq_hz: float) -> None:
        if not (1.0 <= float(freq_hz) <= 500_000_000.0):
            raise ValueError(f"Frequency {freq_hz!r} Hz is outside the safe driver range")

    @staticmethod
    def _validate_duty(duty_pct: float) -> None:
        if not (0.1 <= float(duty_pct) <= 99.9):
            raise ValueError(f"Duty cycle {duty_pct!r}% is outside 0.1..99.9%")

    def _write_or_raise(self, command: str) -> None:
        if not self.client.write(command):
            raise ConnectionError(f"Wavegen command failed: {command}")

    def _output_is_on(self, channel: int) -> Optional[bool]:
        response = self.client.query(f"C{channel}:OUTP?")
        if response is None:
            return None
        normalized = response.upper().replace(" ", "")
        if "OFF" in normalized:
            return False
        if "ON" in normalized:
            return True
        return None

    def configure(self, config: str, freq_hz: float, duty_pct: float) -> None:
        """Apply the full channel setup for a rig configuration. Outputs stay OFF."""
        self._validate_config(config)
        self._validate_frequency(freq_hz)
        self._validate_duty(duty_pct)

        # Once configuration I/O begins the previous verified configuration is
        # no longer trustworthy.  A partial write must not leave a stale cache
        # that a later arm operation could mistake for a fully applied setup.
        with self._state_lock:
            self._configured_config = None
        self.outputs_off()
        try:
            self._write_or_raise(
                "C1:" + _BSWV.format(freq=int(freq_hz), duty=duty_pct)
            )
            time.sleep(0.2)

            if config == "Dual Conduction":
                self._write_or_raise(
                    "C2:" + _BSWV.format(freq=int(freq_hz), duty=duty_pct)
                )
                self._write_or_raise("COUP STATE,ON")
                self._write_or_raise("COUP FCOUP,ON")
                self._write_or_raise("COUP DCOUP,ON")
            else:
                # Always undo coupling left by a previous dual-channel run.
                # This is required for both single-channel configurations.
                self._write_or_raise("COUP STATE,OFF")
                self._write_or_raise("C2:BSWV WVTP,DC,OFST,0")
                time.sleep(0.2)
        except Exception:
            with self._state_lock:
                self._current_freq_hz = None
                self._current_duty_pct = None
            raise

        with self._state_lock:
            self._current_freq_hz = float(freq_hz)
            self._current_duty_pct = float(duty_pct)
            self._configured_config = config

        time.sleep(0.3)
        log.info("Wavegen configured: %s, %.0f Hz, %s%% duty", config, freq_hz, duty_pct)

    def _raw_set_frequency(self, freq_hz: float, dual: bool) -> None:
        self._validate_frequency(freq_hz)
        freq = int(round(freq_hz))
        try:
            self._write_or_raise(f"C1:BSWV FRQ,{freq}")
            if dual:
                self._write_or_raise(f"C2:BSWV FRQ,{freq}")
        except Exception:
            with self._state_lock:
                self._current_freq_hz = None
                self._configured_config = None
            raise
        with self._state_lock:
            self._current_freq_hz = float(freq)

    def _raw_set_duty(self, duty_pct: float, dual: bool) -> None:
        self._validate_duty(duty_pct)
        try:
            self._write_or_raise(f"C1:BSWV DUTY,{duty_pct}")
            if dual:
                self._write_or_raise(f"C2:BSWV DUTY,{duty_pct}")
        except Exception:
            with self._state_lock:
                self._current_duty_pct = None
                self._configured_config = None
            raise
        with self._state_lock:
            self._current_duty_pct = float(duty_pct)

    def set_frequency(self, freq_hz: float, dual: bool) -> None:
        """Set waveform frequency in Hz (routes through goal-based ramp)."""
        self.ramp_to_frequency(freq_hz, dual)

    def ramp_to_frequency(
        self,
        target_freq_hz: float,
        dual: bool,
        *,
        rate_khz_s: Optional[float] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> float:
        """Gradually ramp frequency to target goal at configured rate (kHz/s)."""
        self._validate_frequency(target_freq_hz)
        with self._state_lock:
            start = self._current_freq_hz
            epoch = self._output_epoch
        if start is None:
            start = self.read_frequency()
        if start is None:
            if cancel_check is not None and cancel_check():
                raise RuntimeError(
                    "Frequency ramp cancelled before the current value was known"
                )
            self._raw_set_frequency(target_freq_hz, dual)
            return float(target_freq_hz)
        if abs(target_freq_hz - start) < 1.0:
            if cancel_check is not None and cancel_check():
                return float(start)
            self._raw_set_frequency(target_freq_hz, dual)
            return float(target_freq_hz)

        rate = float(rate_khz_s if rate_khz_s is not None and rate_khz_s > 0 else DEFAULT_FREQ_RATE_KHZ_S)
        step_hz = 10_000.0  # 10 kHz step granularity for smooth rate-limited ramping
        delay = (step_hz / 1000.0) / max(1.0, rate)

        current = float(start)
        target = float(target_freq_hz)
        direction = 1.0 if target > current else -1.0

        while abs(target - current) > 1.0:
            with self._state_lock:
                disarmed_during_ramp = self._output_epoch != epoch
            if disarmed_during_ramp or (
                cancel_check is not None and cancel_check()
            ):
                return current
            if abs(target - current) <= step_hz:
                current = target
            else:
                current += direction * step_hz
            self._raw_set_frequency(current, dual)
            if current != target:
                time.sleep(delay)

        return current

    def set_duty(self, duty_pct: float, dual: bool) -> None:
        """Set waveform duty cycle in percent (routes through goal-based ramp)."""
        self.ramp_to_duty(duty_pct, dual)

    def ramp_to_duty(
        self,
        target_duty_pct: float,
        dual: bool,
        *,
        rate_pct_s: Optional[float] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> float:
        """Gradually ramp duty cycle to target goal at configured rate (%/s)."""
        self._validate_duty(target_duty_pct)
        with self._state_lock:
            start = self._current_duty_pct
            epoch = self._output_epoch
        if start is None:
            if cancel_check is not None and cancel_check():
                raise RuntimeError(
                    "Duty ramp cancelled before the current value was known"
                )
            self._raw_set_duty(target_duty_pct, dual)
            return float(target_duty_pct)
        if abs(target_duty_pct - start) < 0.1:
            if cancel_check is not None and cancel_check():
                return float(start)
            self._raw_set_duty(target_duty_pct, dual)
            return float(target_duty_pct)

        rate = float(rate_pct_s if rate_pct_s is not None and rate_pct_s > 0 else DEFAULT_DUTY_RATE_PCT_S)
        step_pct = 0.5  # 0.5% step granularity for smooth rate-limited ramping
        delay = step_pct / max(0.1, rate)

        current = float(start)
        target = float(target_duty_pct)
        direction = 1.0 if target > current else -1.0

        while abs(target - current) > 0.01:
            with self._state_lock:
                disarmed_during_ramp = self._output_epoch != epoch
            if disarmed_during_ramp or (
                cancel_check is not None and cancel_check()
            ):
                return current
            if abs(target - current) <= step_pct:
                current = target
            else:
                current += direction * step_pct
            self._raw_set_duty(current, dual)
            if current != target:
                time.sleep(delay)

        return current

    def read_frequency(self) -> Optional[float]:
        """Parse the FRQ value out of a C1:BSWV? response."""
        resp = self.client.query("C1:BSWV?")
        if not resp:
            return None
        tokens = [t.strip() for t in resp.split(",")]
        for i, token in enumerate(tokens):
            if token.upper().endswith("FRQ") and i + 1 < len(tokens):
                try:
                    val = float(tokens[i + 1].upper().replace("HZ", ""))
                    with self._state_lock:
                        self._current_freq_hz = val
                    return val
                except ValueError:
                    return None
        return None

    def arm_outputs(self, config: str) -> bool:
        """Arm only the channels required by ``config``, with readback."""
        self._validate_config(config)
        with self._state_lock:
            configured_config = self._configured_config
        if configured_config != config:
            raise RuntimeError(
                f"Wavegen is configured for {configured_config!r}, not {config!r}"
            )
        dual = self.is_dual(config)
        try:
            if dual:
                self._write_or_raise("C2:OUTP ON")
            else:
                self._write_or_raise("C2:OUTP OFF")
            self._write_or_raise("C1:OUTP ON")

            c1_on = self._output_is_on(1)
            c2_on = self._output_is_on(2)
            if c1_on is not True or (c2_on is not dual):
                raise ConnectionError(
                    "Wavegen output readback did not match the requested arm state"
                )
        except Exception:
            try:
                self.outputs_off()
            except Exception:
                log.exception("Wavegen failed to disarm after an arm failure")
            raise

        with self._state_lock:
            self._outputs_armed = True
            self._armed_config = config
        return True

    @property
    def outputs_armed(self) -> bool:
        with self._state_lock:
            return self._outputs_armed

    def outputs_off(self) -> None:
        # Invalidate in-flight ramps before waiting on any transport I/O.
        with self._state_lock:
            self._output_epoch += 1

        errors: list[str] = []
        for command in ("C1:OUTP OFF", "C2:OUTP OFF"):
            try:
                self._write_or_raise(command)
            except Exception as exc:
                errors.append(str(exc))

        for channel in (1, 2):
            state = self._output_is_on(channel)
            if state is not False:
                errors.append(f"C{channel} output-off readback was not confirmed")

        if errors:
            raise ConnectionError("; ".join(errors))

        with self._state_lock:
            self._outputs_armed = False
            self._armed_config = None
