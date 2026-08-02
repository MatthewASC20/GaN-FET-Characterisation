"""Keithley 2400-series SMU (2400/2410/...) — DC bus source AND input-current meter.

Replaces both the manual bench PSU and the legacy Prologix-USB GPIB ammeter.
Reached over the network via a GPIB/serial-to-LAN bridge:

* transparent bridge → plain SCPI over the TCP socket (leave
  `prologix_gpib_addr` unset);
* Prologix GPIB-ETHERNET → set `prologix_gpib_addr`; the transport sends
  the `++` controller framing before any SCPI (default bridge port 1234).

The SMU sources voltage and senses current, so a single `:READ?` yields the
bus voltage at its terminals and the DC input current of the rig.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Optional

from gan_fet.instruments.base import SmuInterface
from gan_fet.scpi.protocol import ScpiSession
from gan_fet.settings import SmuSettings

log = logging.getLogger(__name__)


class SmuLimitError(ValueError):
    """Requested setpoint is outside the configured safe envelope."""


class Keithley2400(SmuInterface):
    def __init__(self, client: ScpiSession, settings: SmuSettings):
        self.client = client
        self.settings = settings
        self._setpoint_v = 0.0
        self._output_on = False
        self._initialized = False

        # Legacy application code constructs every instrument client from an
        # (ip, port) pair and only the SMU settings know the GPIB address.
        # Configure the transport here, before any command can open it.  VISA
        # clients explicitly decline the wrapper because VISA owns addressing.
        gpib_addr = self.settings.prologix_gpib_addr
        if gpib_addr is not None and not getattr(
            self.client, "handles_prologix_framing", False
        ):
            configure = getattr(self.client, "configure_prologix", None)
            if callable(configure):
                configure(gpib_addr)
            elif not getattr(self.client, "is_visa", False):
                log.warning(
                    "SMU client cannot configure Prologix framing; using plain SCPI"
                )

    # -- lifecycle -------------------------------------------------------

    def initialize(self) -> bool:
        """Configure as a voltage source measuring current. Output stays OFF."""
        setup = (
            "*RST",
            "*CLS",
            ":SOUR:FUNC VOLT",
            ":SOUR:VOLT:MODE FIXED",
            f":SOUR:VOLT:RANG {self.settings.max_voltage_v}",
            ":SOUR:VOLT 0",
            ':SENS:FUNC "CURR"',
            f":SENS:CURR:PROT {self.settings.current_compliance_a}",
            ":SENS:CURR:RANG:AUTO ON",
            f":SENS:CURR:NPLC {self.settings.nplc}",
            ":FORM:ELEM VOLT,CURR",
        )
        for cmd in setup:
            if not self.client.write(cmd):
                log.error("SMU initialisation failed at '%s'", cmd)
                return False
        self._setpoint_v = 0.0
        self._output_on = False
        self._initialized = True
        log.info(
            "SMU initialised (limit %.0f V, compliance %.3g A, NPLC %.2g)",
            self.settings.max_voltage_v,
            self.settings.current_compliance_a,
            self.settings.nplc,
        )
        return True

    def ensure_initialized(self) -> bool:
        return self._initialized or self.initialize()

    # -- source ------------------------------------------------------------

    @property
    def setpoint_v(self) -> float:
        return self._setpoint_v

    @property
    def output_is_on(self) -> bool:
        return self._output_on

    @property
    def max_voltage_v(self) -> float:
        return float(self.settings.max_voltage_v)

    @property
    def ramp_step_v(self) -> float:
        return float(self.settings.ramp_step_v)

    def _raw_set_voltage(self, volts: float) -> None:
        if not (0 <= volts <= self.settings.max_voltage_v):
            raise SmuLimitError(
                f"Setpoint {volts:.1f} V outside 0..{self.settings.max_voltage_v:.0f} V"
            )
        if not self.client.write(f":SOUR:VOLT {volts:.4f}"):
            raise ConnectionError("SMU write failed while setting voltage")
        self._setpoint_v = float(volts)

    def set_voltage(self, volts: float) -> None:
        """Set output voltage in Volts (routes through rate-limited ramping)."""
        self.ramp_to(volts)

    def ramp_to(
        self,
        volts: float,
        *,
        step_v: Optional[float] = None,
        delay_s: Optional[float] = None,
        cancel_check=None,
    ) -> None:
        """Soft ramp from the current setpoint to `volts` (all changes are
        rate-limited through here).

        ``ramp_rate_v_s`` is the sole persisted speed control.  The optional
        ``delay_s`` argument is an explicit one-call override retained for API
        compatibility and takes precedence over that configured rate.
        """
        if not (0 <= volts <= self.settings.max_voltage_v):
            raise SmuLimitError(
                f"Setpoint {volts:.1f} V outside 0..{self.settings.max_voltage_v:.0f} V"
            )
        if cancel_check is not None and cancel_check():
            return

        if not self._output_on or abs(volts - self._setpoint_v) < 1e-4:
            self._raw_set_voltage(volts)
            return

        step = abs(step_v if step_v is not None else self.settings.ramp_step_v)
        if step <= 0:
            step = 1.0  # Ensure step is positive for smooth rate-limited ramping
        if delay_s is not None:
            delay = float(delay_s)
            if not math.isfinite(delay) or delay < 0:
                raise ValueError("delay_s must be a finite non-negative number")
        else:
            delay = self.settings.ramp_delay_for_step(step)

        current = self._setpoint_v
        direction = 1 if volts > current else -1
        while abs(volts - current) > step:
            if cancel_check is not None and cancel_check():
                return
            current += direction * step
            self._raw_set_voltage(current)
            if delay > 0:
                time.sleep(delay)
        if cancel_check is not None and cancel_check():
            return
        self._raw_set_voltage(volts)

    def output_on(self) -> bool:
        """Enable the output — always from a 0 V setpoint (soft start)."""
        if not self.ensure_initialized():
            return False
        if not self._output_on:
            self._raw_set_voltage(0.0)
            if not self.client.write(":OUTP ON"):
                return False
        state = self._read_output_state()
        if state is True:
            self._output_on = True
            return True

        # The ON command may have reached the instrument even if readback was
        # lost.  Fail closed and immediately make an independent OFF attempt.
        log.critical("SMU output-on readback was not confirmed")
        self._output_on = state is not False
        self.emergency_off()
        return False

    def output_off(self) -> bool:
        write_ok = self.client.write(":OUTP OFF")
        confirmed_off = self._read_output_state() is False
        ok = write_ok and confirmed_off
        if ok:
            self._output_on = False
            self._setpoint_v = 0.0
        else:
            # With no trustworthy readback, retain the conservative energized
            # state so callers cannot clear an interlock from a stale cache.
            self._output_on = True
            log.critical("SMU output-off readback was not confirmed")
        return ok

    def emergency_off(self) -> bool:
        """Best-effort kill with explicit output-state readback."""
        abort_ok = False
        off_write_ok = False
        try:
            abort_ok = self.client.write(":ABOR")
        except Exception:
            log.exception("SMU abort command failed")
        try:
            off_write_ok = self.client.write(":OUTP OFF")
        except Exception:
            log.exception("SMU output-off command failed")

        confirmed_off = self._read_output_state() is False
        off_ok = off_write_ok and confirmed_off
        if off_ok:
            self._output_on = False
            self._setpoint_v = 0.0
        else:
            self._output_on = True
        if not abort_ok:
            log.warning("SMU abort command was not acknowledged")
        if not off_ok:
            log.critical("SMU output-off command/readback was not confirmed")
        return off_ok

    def _read_output_state(self) -> Optional[bool]:
        try:
            raw = self.client.query(":OUTP?")
        except Exception:
            log.exception("SMU output-state query failed")
            return None
        if raw is None:
            return None
        normalized = raw.strip().upper().lstrip("+")
        if normalized in {"1", "ON"}:
            return True
        if normalized in {"0", "OFF"}:
            return False
        log.warning("SMU: unparseable output-state response: %r", raw)
        return None

    # -- measure -------------------------------------------------------------

    def read(self) -> Optional[tuple[float, float]]:
        """Trigger a measurement; returns (voltage_v, current_a) or None."""
        raw = self.client.query(":READ?")
        if not raw:
            return None
        parts = raw.split(",")
        try:
            return float(parts[0]), float(parts[1])
        except (IndexError, ValueError):
            log.warning("SMU: unparseable :READ? response: %r", raw)
            return None

    def measure_dc_current(self) -> Optional[float]:
        result = self.read()
        return result[1] if result else None

    def compliance_tripped(self) -> Optional[bool]:
        raw = self.client.query(":SENS:CURR:PROT:TRIP?")
        if raw is None:
            return None
        normalized = raw.strip().lstrip("+")
        if normalized.startswith("1"):
            return True
        if normalized.startswith("0"):
            return False
        log.warning("SMU: unparseable compliance response: %r", raw)
        return None

    def go_local(self) -> None:
        request_local = getattr(self.client, "request_local_control", None)
        if callable(request_local):
            request_local()
            return
        # Compatibility with small third-party client doubles.  The fallback
        # is always instrument SCPI; controller-specific text stays out of the
        # driver and can therefore never leak into a direct VISA session.
        self.client.write(":SYST:LOC")


# Alias for Keithley 2410 SMU
Keithley2410 = Keithley2400
