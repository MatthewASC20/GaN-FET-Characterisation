"""Focused regressions for the energized experiment runtime."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

import pytest

from gan_fet.core.events import SampleAcquiredEvent, SafetyTripEvent, bus
from gan_fet.core.experiment import EngineCallbacks, ExperimentEngine
from gan_fet.core.models import ExperimentParams, ExperimentState, MatrixPoint
from gan_fet.core.safety import SafetyMonitor
from gan_fet.instruments.base import (
    OscilloscopeInterface,
    SmuInterface,
    WavegenInterface,
)
from gan_fet.settings import Settings
from gan_fet.storage.db import Database


class FakePlant:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.bus_v = 0.0
        self.smu_on = False
        self.gates_on = False
        self.force_current_a: Optional[float] = None
        self.force_compliance = False
        self.actual_frequency_hz = 6_350_000.0

    def current_a(self) -> float:
        if not self.smu_on:
            return 0.0
        if self.force_current_a is not None:
            return self.force_current_a
        # A visible, bounded minimum at 95 V makes ZVS move away from the
        # 100 V bus point used to establish a 300 V peak-voltage seed.
        return 0.020 + 1e-4 * (self.bus_v - 95.0) ** 2

    def peak_v(self) -> float:
        if not (self.smu_on and self.gates_on):
            return 0.0
        return 3.0 * self.bus_v


class FakeSmu(SmuInterface):
    def __init__(self, plant: FakePlant) -> None:
        self.plant = plant
        self.read_count = 0
        self.current_read_count = 0
        self.output_on_attempts = 0

    def initialize(self) -> bool:
        return True

    @property
    def setpoint_v(self) -> float:
        with self.plant.lock:
            return self.plant.bus_v

    @property
    def output_is_on(self) -> bool:
        with self.plant.lock:
            return self.plant.smu_on

    @property
    def max_voltage_v(self) -> float:
        return 400.0

    @property
    def ramp_step_v(self) -> float:
        return 50.0

    def set_voltage(self, volts: float) -> None:
        with self.plant.lock:
            self.plant.bus_v = float(volts)

    def ramp_to(
        self,
        volts: float,
        *,
        step_v: Optional[float] = None,
        delay_s: Optional[float] = None,
        cancel_check=None,
    ) -> None:
        del step_v, delay_s
        if cancel_check is not None and cancel_check():
            return
        self.set_voltage(volts)

    def output_on(self) -> bool:
        self.output_on_attempts += 1
        with self.plant.lock:
            self.plant.smu_on = True
        return True

    def output_off(self) -> bool:
        with self.plant.lock:
            self.plant.smu_on = False
        return True

    def emergency_off(self) -> bool:
        return self.output_off()

    def read(self) -> tuple[float, float]:
        with self.plant.lock:
            self.read_count += 1
            return self.plant.bus_v, self.plant.current_a()

    def measure_dc_current(self) -> float:
        with self.plant.lock:
            self.current_read_count += 1
            return self.plant.current_a()

    def compliance_tripped(self) -> bool:
        with self.plant.lock:
            return self.plant.force_compliance


class FakeWavegen(WavegenInterface):
    def __init__(self, plant: FakePlant) -> None:
        self.plant = plant
        self.arm_attempts = 0

    @staticmethod
    def is_dual(config: str) -> bool:
        return config == "Dual Conduction"

    def configure(self, config: str, freq_hz: float, duty_pct: float) -> None:
        del config, duty_pct
        with self.plant.lock:
            self.plant.actual_frequency_hz = float(freq_hz)

    def set_frequency(self, freq_hz: float, dual: bool) -> None:
        del dual
        with self.plant.lock:
            self.plant.actual_frequency_hz = float(freq_hz)

    def ramp_to_frequency(
        self,
        target_freq_hz: float,
        dual: bool,
        *,
        rate_khz_s: Optional[float] = None,
        cancel_check=None,
    ) -> float:
        del dual, rate_khz_s, cancel_check
        self.set_frequency(target_freq_hz, False)
        return float(target_freq_hz)

    def set_duty(self, duty_pct: float, dual: bool) -> None:
        del duty_pct, dual

    def ramp_to_duty(
        self,
        target_duty_pct: float,
        dual: bool,
        *,
        rate_pct_s: Optional[float] = None,
        cancel_check=None,
    ) -> float:
        del dual, rate_pct_s, cancel_check
        return float(target_duty_pct)

    def read_frequency(self) -> float:
        with self.plant.lock:
            return self.plant.actual_frequency_hz

    def arm_outputs(self, config: str) -> bool:
        del config
        self.arm_attempts += 1
        with self.plant.lock:
            self.plant.gates_on = True
        return True

    @property
    def outputs_armed(self) -> bool:
        with self.plant.lock:
            return self.plant.gates_on

    def outputs_off(self) -> None:
        with self.plant.lock:
            self.plant.gates_on = False


class FakeScope(OscilloscopeInterface):
    def __init__(self, plant: FakePlant) -> None:
        self.plant = plant

    def verify_identity(self) -> str:
        return "*IDN LECROY,HDO4054,HDO4054SIM,1.0"

    def peak_voltage(self) -> float:
        with self.plant.lock:
            return self.plant.peak_v()

    def rms_current(self) -> float:
        return 1.25

    def isw_rms(self) -> float:
        return 0.75

    def screenshot(self, dest_path: Path) -> None:
        del dest_path
        return None


class WrongIdentityScope(FakeScope):
    def verify_identity(self) -> str:
        raise ConnectionError(
            "Expected LECROY HDO4054, received 'OTHER,NOT-HDO4054,1,1.0'"
        )


class BlockingScreenshotScope(FakeScope):
    """Scope whose capture can be held open to exercise energized polling."""

    def __init__(self, plant: FakePlant) -> None:
        super().__init__(plant)
        self.capture_started = threading.Event()
        self.capture_release = threading.Event()
        self.capture_returned = threading.Event()
        self.concurrent_scope_queries = 0

    def peak_voltage(self) -> float:
        if self.capture_started.is_set() and not self.capture_returned.is_set():
            self.concurrent_scope_queries += 1
        return super().peak_voltage()

    def screenshot(self, dest_path: Path) -> None:
        del dest_path
        self.capture_started.set()
        if not self.capture_release.wait(timeout=3.0):
            raise TimeoutError("test did not release blocking screenshot")
        self.capture_returned.set()


def _runtime_settings(settings: Settings) -> Settings:
    settings.smu.sample_interval_s = 0.01
    settings.peak_control.tolerance_v = 2.0
    settings.peak_control.settle_s = 0.001
    settings.peak_control.max_iterations = 30
    settings.peak_control.min_step_v = 0.1
    settings.peak_control.proportional_gain = 0.5
    settings.zvs.step_v = 1.0
    settings.zvs.settle_s = 0.001
    settings.zvs.samples_per_point = 1
    settings.zvs.min_improvement_a = 0.00005
    settings.zvs.window_v = 20.0
    settings.zvs.max_steps = 30
    settings.safety.max_dc_current_a = 1.0
    settings.safety.max_vds_peak_v = 450.0
    settings.safety.watchdog_consecutive_failures = 2
    return settings


def _point(name: str) -> MatrixPoint:
    return MatrixPoint(
        device_name=name,
        config="Single Device",
        frequency_hz=6_000_000,
        duty_pct=50,
        temperature_c=25,
        voltage_v=300,
    )


def _engine(
    database: Database,
    settings: Settings,
    plant: FakePlant,
    callbacks: Optional[EngineCallbacks] = None,
    *,
    scope: Optional[FakeScope] = None,
) -> tuple[ExperimentEngine, FakeSmu, FakeWavegen]:
    smu = FakeSmu(plant)
    wavegen = FakeWavegen(plant)
    safety = SafetyMonitor(settings.safety, smu, wavegen, database)
    engine = ExperimentEngine(
        database,
        settings,
        smu,
        scope or FakeScope(plant),
        None,
        wavegen,
        safety,
        callbacks=callbacks,
    )
    return engine, smu, wavegen


def test_wrong_scope_identity_prevents_all_energization(
    database: Database,
    settings: Settings,
) -> None:
    configured = _runtime_settings(settings)
    plant = FakePlant()
    engine, smu, wavegen = _engine(
        database,
        configured,
        plant,
        scope=WrongIdentityScope(plant),
    )

    assert engine.start(
        ExperimentParams(_point("WRONG-SCOPE-IDENTITY"), 0.0006)
    )
    outcome = engine.wait_until_idle(timeout=5.0)

    assert outcome is not None
    assert not outcome.success
    assert outcome.status == "failed"
    assert "Expected LECROY HDO4054" in outcome.message
    assert outcome.run_id is not None
    failed_run = database.get_run(outcome.run_id)
    assert failed_run is not None
    assert failed_run.status == "failed"
    assert wavegen.arm_attempts == 0
    assert smu.output_on_attempts == 0
    assert not smu.output_is_on
    assert not wavegen.outputs_armed


def test_zvs_result_completes_and_reports_actual_frequency(
    database: Database,
    settings: Settings,
) -> None:
    configured = _runtime_settings(settings)
    plant = FakePlant()
    engine, smu, wavegen = _engine(database, configured, plant)
    samples: list[SampleAcquiredEvent] = []
    unsubscribe = bus.subscribe(SampleAcquiredEvent, samples.append)
    try:
        assert engine.start(
            ExperimentParams(
                point=_point("ZVS-RUNTIME"),
                duration_minutes=0.0006,
                find_zvs=True,
            )
        )
        outcome = engine.wait_until_idle(timeout=5.0)
    finally:
        unsubscribe()

    assert outcome is not None
    assert outcome.success
    assert outcome.status == "completed"
    assert outcome.record is not None
    assert outcome.record.v_zvs == pytest.approx(95.0)
    assert outcome.record.bus_voltage_v == pytest.approx(95.0)
    # The final 285 V peak is intentionally no longer rejected against the
    # 300 V pre-ZVS search seed.
    assert outcome.record.readings.vds_pk == pytest.approx(285.0)
    assert outcome.record.readings.fsw_hz == pytest.approx(6_350_000.0)
    assert samples
    assert {sample.frequency_hz for sample in samples} == {6_350_000.0}
    assert not smu.output_is_on
    assert not wavegen.outputs_armed


def test_overcurrent_trips_during_peak_control(
    database: Database,
    settings: Settings,
) -> None:
    configured = _runtime_settings(settings)
    plant = FakePlant()
    plant.force_current_a = 1.5
    engine, smu, wavegen = _engine(database, configured, plant)
    events: list[SafetyTripEvent] = []
    unsubscribe = bus.subscribe(SafetyTripEvent, events.append)
    try:
        assert engine.start(
            ExperimentParams(_point("PEAK-OVERCURRENT"), 0.0006)
        )
        outcome = engine.wait_until_idle(timeout=5.0)
    finally:
        unsubscribe()

    assert outcome is not None
    assert not outcome.success
    assert outcome.status == "tripped"
    assert outcome.run_id is not None
    audit = database.safety_events(outcome.run_id)
    assert [row[3] for row in audit] == ["overcurrent"]
    assert len(events) == 1
    assert "overcurrent" in events[0].reason
    assert not smu.output_is_on
    assert not wavegen.outputs_armed


def test_emergency_stop_is_audited_as_trip_not_cancellation(
    database: Database,
    settings: Settings,
) -> None:
    configured = _runtime_settings(settings)
    plant = FakePlant()
    sample_seen = threading.Event()
    engine, smu, wavegen = _engine(database, configured, plant)
    unsubscribe = bus.subscribe(SampleAcquiredEvent, lambda _event: sample_seen.set())
    try:
        assert engine.start(
            ExperimentParams(_point("ESTOP-RUNTIME"), 0.05)
        )
        assert sample_seen.wait(timeout=2.0)
        shutdown_thread = engine.request_emergency_stop()
        shutdown_thread.join(timeout=2.0)
        assert not shutdown_thread.is_alive()
        outcome = engine.wait_until_idle(timeout=5.0)
    finally:
        unsubscribe()

    assert outcome is not None
    assert not outcome.success
    assert outcome.status == "tripped"
    assert "estop" in outcome.message.lower()
    assert outcome.run_id is not None
    audit = database.safety_events(outcome.run_id)
    assert len(audit) == 1
    assert audit[0][3] == "estop"
    assert not smu.output_is_on
    assert not wavegen.outputs_armed


def test_pause_keeps_safety_polling_and_cancel_shuts_outputs_down(
    database: Database,
    settings: Settings,
) -> None:
    configured = _runtime_settings(settings)
    plant = FakePlant()
    engine, smu, wavegen = _engine(database, configured, plant)
    samples: list[SampleAcquiredEvent] = []
    sample_seen = threading.Event()

    def on_sample(event: SampleAcquiredEvent) -> None:
        samples.append(event)
        sample_seen.set()

    unsubscribe = bus.subscribe(SampleAcquiredEvent, on_sample)
    try:
        assert engine.start(
            ExperimentParams(_point("PAUSE-CANCEL"), 0.05)
        )
        assert sample_seen.wait(timeout=2.0)
        engine.toggle_pause()
        assert engine.state == ExperimentState.PAUSED
        sample_count = len(samples)
        safety_reads = smu.read_count
        time.sleep(0.04)
        assert len(samples) == sample_count
        assert smu.read_count > safety_reads

        engine.cancel()
        outcome = engine.wait_until_idle(timeout=5.0)
    finally:
        unsubscribe()

    assert outcome is not None
    assert not outcome.success
    assert outcome.status == "cancelled"
    assert engine.state == ExperimentState.IDLE
    assert not smu.output_is_on
    assert not wavegen.outputs_armed


def test_blocking_screenshot_keeps_overcurrent_interlock_live(
    database: Database,
    settings: Settings,
) -> None:
    configured = _runtime_settings(settings)
    plant = FakePlant()
    scope = BlockingScreenshotScope(plant)
    engine, smu, wavegen = _engine(
        database,
        configured,
        plant,
        scope=scope,
    )
    trip_seen = threading.Event()
    unsubscribe = bus.subscribe(SafetyTripEvent, lambda _event: trip_seen.set())
    try:
        assert engine.start(
            ExperimentParams(_point("CAPTURE-OVERCURRENT"), 0.0006)
        )
        assert scope.capture_started.wait(timeout=2.0)
        reads_before_fault = smu.current_read_count
        with plant.lock:
            plant.force_current_a = 1.5

        # The scope transfer is still blocked, but the independent SMU poll
        # must trip and remove power without waiting for it to return.
        assert trip_seen.wait(timeout=2.0)
        assert not scope.capture_returned.is_set()
        assert smu.current_read_count > reads_before_fault
        assert not smu.output_is_on
        assert not wavegen.outputs_armed
    finally:
        scope.capture_release.set()
        unsubscribe()

    outcome = engine.wait_until_idle(timeout=5.0)
    assert outcome is not None
    assert not outcome.success
    assert outcome.status == "tripped"
    assert outcome.run_id is not None
    assert [row[3] for row in database.safety_events(outcome.run_id)] == [
        "overcurrent"
    ]
    assert scope.capture_returned.is_set()
    assert scope.concurrent_scope_queries == 0
    assert not any(
        thread.name.startswith("scope-screenshot-")
        for thread in threading.enumerate()
    )


def test_cancel_during_blocking_screenshot_joins_capture_worker(
    database: Database,
    settings: Settings,
) -> None:
    configured = _runtime_settings(settings)
    plant = FakePlant()
    scope = BlockingScreenshotScope(plant)
    engine, smu, wavegen = _engine(
        database,
        configured,
        plant,
        scope=scope,
    )

    assert engine.start(ExperimentParams(_point("CAPTURE-CANCEL"), 0.0006))
    assert scope.capture_started.wait(timeout=2.0)
    engine.cancel()
    scope.capture_release.set()
    outcome = engine.wait_until_idle(timeout=5.0)

    assert outcome is not None
    assert not outcome.success
    assert outcome.status == "cancelled"
    assert scope.capture_returned.is_set()
    assert scope.concurrent_scope_queries == 0
    assert not smu.output_is_on
    assert not wavegen.outputs_armed
    assert not any(
        thread.name.startswith("scope-screenshot-")
        for thread in threading.enumerate()
    )


def test_immediate_estop_backfills_audit_after_run_creation(
    database: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = _runtime_settings(settings)
    plant = FakePlant()
    engine, smu, wavegen = _engine(database, configured, plant)
    create_entered = threading.Event()
    release_create = threading.Event()
    original_create = database.create_run

    # This older standalone E-stop must not be mistaken for the event emitted
    # by the invocation under test.
    database.add_safety_event(None, "estop", "older standalone event")

    def delayed_create(
        point: MatrixPoint,
        duration_minutes: float,
        *,
        status: str = "running",
        started_at: Optional[str] = None,
        replace_existing: bool = True,
    ) -> int:
        create_entered.set()
        if not release_create.wait(timeout=3.0):
            raise TimeoutError("test did not release run creation")
        return original_create(
            point,
            duration_minutes,
            status=status,
            started_at=started_at,
            replace_existing=replace_existing,
        )

    monkeypatch.setattr(database, "create_run", delayed_create)
    shutdown_thread: Optional[threading.Thread] = None
    try:
        assert engine.start(
            ExperimentParams(_point("ESTOP-IMMEDIATE"), 0.05)
        )
        assert create_entered.wait(timeout=2.0)

        # The E-stop is latched and audited while create_run is still blocked,
        # so SafetyMonitor cannot yet know the run id.
        shutdown_thread = engine.request_emergency_stop()
    finally:
        release_create.set()

    assert shutdown_thread is not None
    shutdown_thread.join(timeout=3.0)
    assert not shutdown_thread.is_alive()
    outcome = engine.wait_until_idle(timeout=5.0)

    assert outcome is not None
    assert not outcome.success
    assert outcome.status == "tripped"
    assert outcome.run_id is not None
    associated = database.safety_events(outcome.run_id)
    assert len(associated) == 1
    assert associated[0][3:] == ("estop", "operator emergency stop")
    older = [
        row
        for row in database.safety_events()
        if row[4] == "older standalone event"
    ]
    assert len(older) == 1
    assert older[0][1] is None
    assert not smu.output_is_on
    assert not wavegen.outputs_armed
