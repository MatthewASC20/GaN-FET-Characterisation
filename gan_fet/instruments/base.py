"""Hardware Abstraction Layer (HAL) interfaces and InstrumentFactory.

Provides polymorphic Abstract Base Classes (ABCs) for all bench instruments:
- OscilloscopeInterface
- SmuInterface
- WavegenInterface
- InstrumentFactory
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from gan_fet.scpi.protocol import ScpiSession
from gan_fet.settings import SmuSettings

log = logging.getLogger(__name__)


class OscilloscopeInterface(ABC):
    """Abstract interface for bench oscilloscopes."""

    @abstractmethod
    def verify_identity(self) -> str:
        """Return the validated HDO4054 identity or raise on a mismatch."""
        ...

    @abstractmethod
    def peak_voltage(self) -> Optional[float]:
        """Query Vds peak voltage."""
        ...

    @abstractmethod
    def rms_current(self) -> Optional[float]:
        """Query RMS current."""
        ...

    @abstractmethod
    def isw_rms(self) -> Optional[float]:
        """Query switch-current RMS."""
        ...

    @abstractmethod
    def screenshot(self, dest_path: Path) -> Optional[Path]:
        """Capture screenshot PNG of scope display."""
        ...


class MultimeterInterface(ABC):
    """Abstract interface for an independent DC-voltage meter."""

    @abstractmethod
    def dc_voltage(self) -> Optional[float]:
        """Read the rig DC input voltage."""
        ...


class SmuInterface(ABC):
    """Abstract interface for SourceMeter Units (SMU)."""

    @abstractmethod
    def initialize(self) -> bool:
        """Initialize SMU hardware settings."""
        ...

    @property
    @abstractmethod
    def setpoint_v(self) -> float:
        """Current voltage setpoint in Volts."""
        ...

    @property
    @abstractmethod
    def output_is_on(self) -> bool:
        """True if output is enabled."""
        ...

    @property
    @abstractmethod
    def max_voltage_v(self) -> float:
        """Configured maximum source voltage."""
        ...

    @property
    @abstractmethod
    def ramp_step_v(self) -> float:
        """Default voltage-ramp step."""
        ...

    @abstractmethod
    def set_voltage(self, volts: float) -> None:
        """Set output voltage in Volts."""
        ...

    @abstractmethod
    def ramp_to(
        self,
        volts: float,
        *,
        step_v: Optional[float] = None,
        delay_s: Optional[float] = None,
        cancel_check=None,
    ) -> None:
        """Soft ramp voltage to target setpoint."""
        ...

    @abstractmethod
    def output_on(self) -> bool:
        """Enable SMU output."""
        ...

    @abstractmethod
    def output_off(self) -> bool:
        """Disable SMU output."""
        ...

    @abstractmethod
    def emergency_off(self) -> bool:
        """Best-effort emergency shutdown.

        Returns ``True`` only when the driver successfully sent the output-off
        command.  Callers must treat ``False`` as an unknown hardware state.
        """
        ...

    @abstractmethod
    def read(self) -> Optional[Tuple[float, float]]:
        """Read (voltage_v, current_a)."""
        ...

    @abstractmethod
    def measure_dc_current(self) -> Optional[float]:
        """Read DC input current in Amps."""
        ...

    @abstractmethod
    def compliance_tripped(self) -> Optional[bool]:
        """Compliance state, or ``None`` when it cannot be read."""
        ...


class WavegenInterface(ABC):
    """Abstract interface for Arbitrary Waveform Generators (AWG)."""

    @staticmethod
    @abstractmethod
    def is_dual(config: str) -> bool:
        """Return whether ``config`` requires both output channels."""
        ...

    @abstractmethod
    def configure(self, config: str, freq_hz: float, duty_pct: float) -> None:
        """Configure channels for test run."""
        ...

    @abstractmethod
    def set_frequency(self, freq_hz: float, dual: bool) -> None:
        """Set waveform frequency in Hz (ramps to goal at configured rate)."""
        ...

    @abstractmethod
    def ramp_to_frequency(
        self,
        target_freq_hz: float,
        dual: bool,
        *,
        rate_khz_s: Optional[float] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> float:
        """Ramp frequency and return the last successfully applied value."""
        ...

    @abstractmethod
    def set_duty(self, duty_pct: float, dual: bool) -> None:
        """Set waveform duty cycle in percent (ramps to goal at configured rate)."""
        ...

    @abstractmethod
    def ramp_to_duty(
        self,
        target_duty_pct: float,
        dual: bool,
        *,
        rate_pct_s: Optional[float] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> float:
        """Ramp duty cycle and return the last successfully applied value."""
        ...

    @abstractmethod
    def read_frequency(self) -> Optional[float]:
        """Return the channel-1 output frequency, or ``None`` on read failure."""
        ...

    @abstractmethod
    def arm_outputs(self, config: str) -> bool:
        """Enable the output channel(s) required by ``config``.

        Returns ``True`` only after every required command succeeds.
        """
        ...

    @property
    @abstractmethod
    def outputs_armed(self) -> bool:
        """Whether the driver has verified a successful arm operation."""
        ...

    @abstractmethod
    def outputs_off(self) -> None:
        """Turn off all wavegen outputs."""
        ...

class InstrumentFactory:
    """Factory creating HAL instrument driver instances based on configuration."""

    @staticmethod
    def create_oscilloscope(client: ScpiSession) -> OscilloscopeInterface:
        from gan_fet.instruments.oscilloscope import LeCroyHdo4054

        log.info("Creating Teledyne LeCroy HDO4054 oscilloscope driver")
        return LeCroyHdo4054(client)

    @staticmethod
    def create_smu(client: Any, settings: SmuSettings) -> SmuInterface:
        from gan_fet.instruments.smu import Keithley2410
        log.info("Creating Keithley 2410/2400 SMU driver")
        return Keithley2410(client, settings)

    @staticmethod
    def create_wavegen(client: Any, settings: Optional[Any] = None) -> WavegenInterface:
        from gan_fet.instruments.wavegen import Sdg6022x
        log.info("Creating Siglent SDG6022X wavegen driver")
        return Sdg6022x(client)
