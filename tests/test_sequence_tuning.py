"""What a sequenced run asks the engine to do.

Reported from a simulated sequence: it never tuned the gate frequency. The
cause was that ``AutoSequence`` built its own ``ExperimentParams`` and set only
``tune_voltage``, so ``tune_frequency`` took its dataclass default of False.
Single runs and the 6-second validation both passed it, so the flag worked
everywhere except the one place that runs for hours unattended.

Nothing failed visibly, because the step before the run recalls a previously
measured tuned frequency and ramps to it. That looks like tuning, and on a
device with history it is nearly as good. On a fresh device there is nothing to
recall, so every point in the matrix ran at its nominal frequency with
``tuned_frequency_hz`` NULL — a whole characterisation set taken off resonance.

These tests watch what reaches the engine.
"""

from __future__ import annotations

import threading
from typing import Optional

from gan_fet.core.models import ExperimentParams, MatrixPoint
from gan_fet.core.sequence import AutoSequence, SequenceCallbacks


def _point(voltage: int = 200) -> MatrixPoint:
    return MatrixPoint(
        device_name="EPC2001C",
        config="Single Device",
        frequency_hz=6_000_000,
        duty_pct=50,
        temperature_c=25,
        voltage_v=voltage,
    )


class _Outcome:
    success = True
    message = ""
    record = None
    run_id = 1


class _RecordingEngine:
    """Accepts every run and remembers the params it was given."""

    def __init__(self) -> None:
        self.params: list[ExperimentParams] = []
        self.safety = type("_S", (), {"is_tripped": False})()

    def is_busy(self) -> bool:
        return False

    def start(self, params: ExperimentParams) -> bool:
        self.params.append(params)
        return True

    def wait_until_idle(self):
        return _Outcome()

    def cancel(self) -> None:
        return None


class _StubWavegen:
    def apply(self, *_args, **_kwargs) -> None:
        return None

    def ramp_to_frequency(self, *_args, **_kwargs) -> None:
        return None

    def disarm_outputs(self) -> None:
        return None


class _StubDb:
    """No prior runs, so nothing can be recalled — the fresh-device case."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def prior_tuned_frequency(self, *_args, **_kwargs) -> Optional[tuple]:
        return None


def _run_sequence(points, *, tune_voltage: bool = False) -> _RecordingEngine:
    engine = _RecordingEngine()
    sequence = AutoSequence(
        db=_StubDb(),
        engine=engine,
        wavegen_controller=_StubWavegen(),
        all_configs=["Single Device"],
        callbacks=SequenceCallbacks(),
    )
    assert sequence.start(list(points), 1.0, tune_voltage)
    assert sequence.join(timeout=10.0), "sequence worker did not stop"
    return engine


def test_a_sequenced_run_tunes_the_frequency():
    """The regression. Every point in a characterisation matrix is measured at
    the frequency found for it, not at the nominal one."""
    engine = _run_sequence([_point()])

    assert len(engine.params) == 1
    assert engine.params[0].tune_frequency is True


def test_every_point_tunes_not_just_the_first():
    """Coss moves resonance with amplitude, so each voltage has its own
    resonance. Tuning once and reusing it across the plan would put every
    subsequent point off resonance by a different amount."""
    engine = _run_sequence([_point(100), _point(200), _point(300)])

    assert len(engine.params) == 3
    assert all(params.tune_frequency for params in engine.params)


def test_tuning_frequency_does_not_depend_on_the_voltage_tune_setting():
    """Two independent operations. Conflating them is how the frequency flag
    went missing in the first place — the sequence carried one and silently
    dropped the other."""
    off = _run_sequence([_point()], tune_voltage=False)
    on = _run_sequence([_point()], tune_voltage=True)

    assert off.params[0].tune_frequency is True
    assert on.params[0].tune_frequency is True
    assert off.params[0].tune_voltage is False
    assert on.params[0].tune_voltage is True


def test_the_voltage_tune_choice_still_reaches_the_engine():
    """Guarding the fix: threading the frequency flag through must not knock
    out the flag that was already working."""
    assert _run_sequence([_point()], tune_voltage=True).params[0].tune_voltage is True
    assert _run_sequence([_point()], tune_voltage=False).params[0].tune_voltage is False


def test_the_point_and_duration_are_passed_through_unchanged():
    """The rest of the params are what the plan asked for."""
    engine = _run_sequence([_point(350)])

    assert engine.params[0].point.voltage_v == 350
    assert engine.params[0].duration_minutes == 1.0
