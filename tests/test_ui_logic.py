"""Headless tests for UI policies and staging logic.

These tests deliberately avoid creating a Tk root, so they run on build hosts
without a window server.
"""

from types import SimpleNamespace

import pytest

from gan_fet.core.safety import SafetyTrip
from gan_fet.core.models import FinalReadings, RunRecord
from gan_fet.settings import SCOPE_INSTRUMENT_KEY, InstrumentAddress
from gan_fet.ui.command_log_view import rows_over_limit, single_line_text
from gan_fet.ui.config_tab import (
    InstrumentConfigEditor,
    RampRatesSliderEditor,
    SmuLimitsEditor,
    options_without_indices,
    validate_instrument_target,
    validate_optional_prologix_address,
)
from gan_fet.ui.main_window import (
    EXPERIMENT_CONTROL_COLUMNS,
    EXPERIMENT_PLOT_COLUMN,
    EXPERIMENT_PLOT_ROWSPAN,
    ExperimentRow,
    MainWindow,
    SIMULATION_VALIDATION_DURATION_MINUTES,
    mode_banner_presentation,
)
from gan_fet.ui.panels.smu_panel import SmuPanel
from gan_fet.ui.tracker_view import format_run_details
from gan_fet.ui.widgets import (
    RigControlState,
    OperationCoordinator,
    resolve_confirm_presentation,
    resolve_rig_control_state,
)


class _HeadlessWindow(MainWindow):
    """``MainWindow`` with Tk's attribute forwarding disabled.

    ``tk.Misc.__getattr__`` forwards unknown attributes to ``self.tk``. On an
    instance built with ``object.__new__`` — never initialised, so ``tk`` is
    absent too — that recurses until the stack blows, and a test double missing
    one attribute fails with ``RecursionError`` naming nothing.

    Raising ``AttributeError`` instead names the attribute, and lets the
    ``hasattr`` guards in the window behave as they do in a real session.
    """

    def __getattr__(self, name):  # pragma: no cover - diagnostics only
        raise AttributeError(
            f"{type(self).__name__} test double has no attribute {name!r}; "
            "set it on the double if the code under test now needs it"
        )


def _headless_window() -> MainWindow:
    """A window instance with no Tk root, for exercising pure methods."""
    return object.__new__(_HeadlessWindow)


class _FakeWidget:
    def __init__(self) -> None:
        self.options = {}

    def config(self, **kwargs) -> None:
        self.options.update(kwargs)


class _FakeSmuPanel:
    """Stands in for SmuPanel, which cannot be built without a Tk root."""

    def __init__(self) -> None:
        self.zvs_button = _FakeWidget()
        self.bus_off_button = _FakeWidget()
        self.reset_safety_button = _FakeWidget()
        self.estop_button = _FakeWidget()

    def set_zvs_stopping(self) -> None:
        self.zvs_button.config(text="Stopping...", state="disabled")

    def apply_control_state(self, controls) -> None:
        SmuPanel.apply_control_state(self, controls)


class _FakeVar:
    def __init__(self, value) -> None:
        self.value = value

    def get(self):
        return self.value

    def set(self, value) -> None:
        self.value = value


class _FakeSettings:
    def __init__(self) -> None:
        self.smu = SimpleNamespace(ramp_rate_v_s=25.0)
        self.zvs = SimpleNamespace(
            autotune_freq_rate_khz_s=200.0,
            duty_ramp_rate_pct_s=10.0,
        )
        self.wavegen = SimpleNamespace(
            freq_ramp_rate_khz_s=200.0,
            duty_ramp_rate_pct_s=10.0,
        )
        self.save_calls = 0

    def save(self) -> None:
        self.save_calls += 1


def _headless_ramp_editor(settings: _FakeSettings) -> RampRatesSliderEditor:
    editor = object.__new__(RampRatesSliderEditor)
    editor.settings = settings
    editor._dirty = False
    editor.v_label = _FakeWidget()
    editor.freq_label = _FakeWidget()
    editor.duty_label = _FakeWidget()
    editor.apply_button = _FakeWidget()
    return editor


def test_experiment_grid_rows_do_not_overlap() -> None:
    parameter_rows = set(
        range(int(ExperimentRow.PARAMETER_FIRST), int(ExperimentRow.RUN_OPTIONS))
    )
    later_control_rows = {
        int(ExperimentRow.RUN_OPTIONS),
        int(ExperimentRow.RUN_BUTTONS),
        int(ExperimentRow.TELEMETRY),
        int(ExperimentRow.SMU),
        int(ExperimentRow.ACTIONS),
        int(ExperimentRow.FLEX_SPACER),
    }

    assert parameter_rows.isdisjoint(later_control_rows)
    assert ExperimentRow.RUN_BUTTONS != ExperimentRow.TELEMETRY
    assert EXPERIMENT_PLOT_COLUMN == EXPERIMENT_CONTROL_COLUMNS
    assert EXPERIMENT_PLOT_ROWSPAN == int(ExperimentRow.FLEX_SPACER) + 1


def test_mode_banner_makes_virtual_and_live_operation_unambiguous() -> None:
    simulated_text, simulated_colour = mode_banner_presentation(True)
    live_text, live_colour = mode_banner_presentation(False)

    assert "SIMULATION" in simulated_text
    assert "NO BENCH I/O" in simulated_text
    assert "NOT MEASURED DATA" in simulated_text
    assert "LIVE HARDWARE" in live_text
    assert simulated_colour != live_colour


def test_simulation_validation_launches_normal_engine_with_short_duration(
    matrix_point,
) -> None:
    window = _headless_window()
    window.is_simulated = True
    window._simulation_validation_active = False
    window._safety_is_tripped = lambda: False
    window.operations = SimpleNamespace(busy=False)
    window._validated_current_point = lambda: matrix_point
    window.find_zvs_var = _FakeVar(True)
    window.tune_frequency_var = _FakeVar(True)
    window.wavegen_controller = SimpleNamespace(
        has_pending_changes=lambda *_args: False
    )
    messages = []
    window.status_bar = SimpleNamespace(set_message=messages.append)
    launched = []
    window._launch_experiment = lambda params: launched.append(params) or True

    MainWindow._run_simulation_validation(window)

    assert len(launched) == 1
    assert launched[0].point == matrix_point
    assert launched[0].duration_minutes == SIMULATION_VALIDATION_DURATION_MINUTES
    assert launched[0].find_zvs
    # The validation is the production path, so it must carry the frequency
    # search too rather than quietly running at nominal.
    assert launched[0].tune_frequency
    assert window._simulation_validation_active
    assert "normal experiment workflow" in messages[-1]


def test_run_details_include_all_collected_readings(matrix_point) -> None:
    run = RunRecord(
        id=7,
        point=matrix_point,
        duration_minutes=0.1,
        started_at="start",
        completed_at="finish",
        status="completed",
        bus_voltage_v=66.7,
        readings=FinalReadings(
            vin=66.7,
            iin=0.021,
            fsw_hz=13_000_000,
            irms=1.7,
            vds_pk=200.0,
            isw_rms=0.8,
        ),
        screenshot_path="simulation/scope.png",
    )

    details = format_run_details(run, 12)

    assert "Samples: 12" in details
    assert "Vds peak: 200" in details
    assert "Switching frequency: 1.3e+07 Hz" in details
    assert "simulation/scope.png" in details


def test_offline_policy_blocks_rig_actions_but_keeps_configuration_usable() -> None:
    state = resolve_rig_control_state(
        active_kind=None,
        closing=False,
        safety_tripped=False,
        hardware_offline=True,
        engine_running=False,
    )

    assert state.edit_inputs
    assert state.local_actions
    assert state.configuration
    assert not state.hardware_actions
    assert not state.shutdown_actions
    assert not state.reset_safety


def test_frequency_actions_are_denied_while_the_bus_is_energised() -> None:
    """Autotune moves the gate frequency with no closed-loop peak control.

    Frequency shifts the resonant operating point and therefore Vds peak, so a
    standalone move is confined to a de-energised bus. The in-run frequency
    search is a separate path that holds the peak on target throughout.
    """
    live = resolve_rig_control_state(
        active_kind=None,
        closing=False,
        safety_tripped=False,
        hardware_offline=False,
        engine_running=False,
        bus_energised=True,
    )
    assert not live.frequency_actions
    # Specific to frequency: other hardware actions stay available.
    assert live.hardware_actions

    off = resolve_rig_control_state(
        active_kind=None,
        closing=False,
        safety_tripped=False,
        hardware_offline=False,
        engine_running=False,
        bus_energised=False,
    )
    assert off.frequency_actions


def test_frequency_actions_inherit_every_hardware_restriction() -> None:
    for overrides in (
        {"safety_tripped": True},
        {"hardware_offline": True},
        {"active_kind": "zvs"},
        {"closing": True},
    ):
        kwargs = dict(
            active_kind=None,
            closing=False,
            safety_tripped=False,
            hardware_offline=False,
            engine_running=False,
            bus_energised=False,
        )
        kwargs.update(overrides)
        assert not resolve_rig_control_state(**kwargs).frequency_actions, overrides


def test_control_policy_preserves_safety_and_cancellation_paths() -> None:
    tripped = resolve_rig_control_state(
        active_kind=None,
        closing=False,
        safety_tripped=True,
        hardware_offline=False,
        engine_running=False,
    )
    assert not tripped.hardware_actions
    assert tripped.shutdown_actions
    assert tripped.reset_safety

    sequence = resolve_rig_control_state(
        active_kind="sequence",
        closing=False,
        safety_tripped=False,
        hardware_offline=False,
        engine_running=False,
    )
    assert sequence.stop_sequence
    assert sequence.cancel_operation
    assert not sequence.configuration

    zvs = resolve_rig_control_state(
        active_kind="zvs",
        closing=False,
        safety_tripped=False,
        hardware_offline=False,
        engine_running=False,
    )
    assert zvs.stop_zvs
    assert zvs.cancel_operation
    assert not zvs.hardware_actions


def test_manual_zvs_button_requests_cooperative_stop_before_hardware_checks() -> None:
    window = _headless_window()
    window.operations = OperationCoordinator()
    token = window.operations.try_begin("zvs")
    assert token is not None
    window.smu_panel = _FakeSmuPanel()
    messages = []
    window.status_bar = SimpleNamespace(set_message=messages.append)
    window._ensure_hardware_online = lambda _action: pytest.fail(
        "stopping ZVS must not be treated as a new hardware action"
    )

    MainWindow._find_zvs_now(window)

    assert token.cancel_event.is_set()
    assert window.smu_panel.zvs_button.options == {
        "text": "Stopping...",
        "state": "disabled",
    }
    assert messages == ["Stopping ZVS search safely..."]


def test_manual_zvs_wrong_scope_identity_never_energizes() -> None:
    token = OperationCoordinator().try_begin("zvs")
    assert token is not None

    class _WrongScope:
        def verify_identity(self) -> str:
            raise ConnectionError(
                "Expected LECROY HDO4054, received 'LECROY,HDO4054A,1,1.0'"
            )

    class _Safety:
        trip_reason = None

        def __init__(self) -> None:
            self.arm_attempts = 0
            self.bus_enable_attempts = 0
            self.shutdown_attempts = 0

        def arm_wavegen(self, _config: str) -> bool:
            self.arm_attempts += 1
            return True

        def enable_bus(self) -> bool:
            self.bus_enable_attempts += 1
            return True

        def shutdown_outputs(self) -> bool:
            self.shutdown_attempts += 1
            return True

        def emergency_stop(self) -> bool:
            return True

    safety = _Safety()
    callbacks = []
    window = _headless_window()
    window._begin_operation = lambda _kind: token
    window._start_worker = lambda worker, **_kwargs: worker()
    window._status_async = lambda _message: None
    window.voltage_var = _FakeVar("300")
    window.config_var = _FakeVar("Single Device")
    window.status_bar = SimpleNamespace(set_message=lambda _message: None)
    window.engine = SimpleNamespace(scope=_WrongScope())
    window.safety = safety
    window.wavegen_controller = SimpleNamespace(outputs_armed=False)
    window.ui_dispatcher = SimpleNamespace(
        post=lambda *args: callbacks.append(args)
    )

    MainWindow._launch_zvs(window)

    assert safety.arm_attempts == 0
    assert safety.bus_enable_attempts == 0
    assert safety.shutdown_attempts == 1
    assert len(callbacks) == 1
    assert isinstance(callbacks[0][-1], ConnectionError)


def test_manual_zvs_stops_before_energizing_when_cancelled_after_arming() -> None:
    """The interlock guard sits between every energising step, not just at the
    top, because cancellation and trips can arrive *during* the previous step.

    Here the operator presses Stop while the wavegen is arming. The bus must
    never be enabled, and the outputs must be made safe on the way out.
    """
    coordinator = OperationCoordinator()
    token = coordinator.try_begin("zvs")
    assert token is not None

    class _Safety:
        trip_reason = None

        def __init__(self) -> None:
            self.bus_enable_attempts = 0
            self.shutdown_attempts = 0

        def arm_wavegen(self, _config: str) -> bool:
            # Cancellation lands while this step is in flight.
            token.cancel_event.set()
            return True

        def enable_bus(self) -> bool:
            self.bus_enable_attempts += 1
            return True

        def shutdown_outputs(self) -> bool:
            self.shutdown_attempts += 1
            return True

        def emergency_stop(self) -> bool:
            return True

    safety = _Safety()
    callbacks = []
    window = _headless_window()
    window._begin_operation = lambda _kind: token
    window._start_worker = lambda worker, **_kwargs: worker()
    window._status_async = lambda _message: None
    window.voltage_var = _FakeVar("300")
    window.config_var = _FakeVar("Single Device")
    window.status_bar = SimpleNamespace(set_message=lambda _message: None)
    window.engine = SimpleNamespace(
        scope=SimpleNamespace(verify_identity=lambda: "LECROY,HDO4054,1,1.0")
    )
    window.safety = safety
    window.wavegen_controller = SimpleNamespace(outputs_armed=True)
    window.ui_dispatcher = SimpleNamespace(
        post=lambda *args: callbacks.append(args)
    )

    MainWindow._launch_zvs(window)

    assert safety.bus_enable_attempts == 0, (
        "the bus was energised after the search had been cancelled"
    )
    assert safety.shutdown_attempts == 1
    assert isinstance(callbacks[0][-1], InterruptedError)


def test_manual_zvs_stops_before_arming_when_cancelled_during_identity() -> None:
    """Verifying the scope takes a round trip over the network, which is long
    enough for Stop to land. Nothing may be armed after that."""
    coordinator = OperationCoordinator()
    token = coordinator.try_begin("zvs")
    assert token is not None

    class _Safety:
        trip_reason = None

        def __init__(self) -> None:
            self.arm_attempts = 0
            self.bus_enable_attempts = 0
            self.shutdown_attempts = 0

        def arm_wavegen(self, _config: str) -> bool:
            self.arm_attempts += 1
            return True

        def enable_bus(self) -> bool:
            self.bus_enable_attempts += 1
            return True

        def shutdown_outputs(self) -> bool:
            self.shutdown_attempts += 1
            return True

        def emergency_stop(self) -> bool:
            return True

    def verify_identity() -> str:
        token.cancel_event.set()
        return "LECROY,HDO4054,1,1.0"

    safety = _Safety()
    callbacks = []
    window = _headless_window()
    window._begin_operation = lambda _kind: token
    window._start_worker = lambda worker, **_kwargs: worker()
    window._status_async = lambda _message: None
    window.voltage_var = _FakeVar("300")
    window.config_var = _FakeVar("Single Device")
    window.status_bar = SimpleNamespace(set_message=lambda _message: None)
    window.engine = SimpleNamespace(
        scope=SimpleNamespace(verify_identity=verify_identity)
    )
    window.safety = safety
    window.wavegen_controller = SimpleNamespace(outputs_armed=True)
    window.ui_dispatcher = SimpleNamespace(
        post=lambda *args: callbacks.append(args)
    )

    MainWindow._launch_zvs(window)

    assert safety.arm_attempts == 0, (
        "the wavegen was armed after the search had been cancelled"
    )
    assert safety.bus_enable_attempts == 0
    assert isinstance(callbacks[0][-1], InterruptedError)


def test_manual_zvs_stops_before_energizing_when_a_trip_lands_mid_arm() -> None:
    """A trip can be latched by the safety monitor from its own polling while
    an operation is in flight. The sequence must notice before the next step
    rather than run on a rig that has already been judged unsafe."""
    coordinator = OperationCoordinator()
    token = coordinator.try_begin("zvs")
    assert token is not None

    class _Safety:
        def __init__(self) -> None:
            self.trip_reason = None
            self.bus_enable_attempts = 0
            self.shutdown_attempts = 0

        def arm_wavegen(self, _config: str) -> bool:
            self.trip_reason = ("overcurrent", "DC input current exceeded limit")
            return True

        def enable_bus(self) -> bool:
            self.bus_enable_attempts += 1
            return True

        def shutdown_outputs(self) -> bool:
            self.shutdown_attempts += 1
            return True

        def emergency_stop(self) -> bool:
            return True

    safety = _Safety()
    callbacks = []
    window = _headless_window()
    window._begin_operation = lambda _kind: token
    window._start_worker = lambda worker, **_kwargs: worker()
    window._status_async = lambda _message: None
    window.voltage_var = _FakeVar("300")
    window.config_var = _FakeVar("Single Device")
    window.status_bar = SimpleNamespace(set_message=lambda _message: None)
    window.engine = SimpleNamespace(
        scope=SimpleNamespace(verify_identity=lambda: "LECROY,HDO4054,1,1.0")
    )
    window.safety = safety
    window.wavegen_controller = SimpleNamespace(outputs_armed=True)
    window.ui_dispatcher = SimpleNamespace(
        post=lambda *args: callbacks.append(args)
    )

    MainWindow._launch_zvs(window)

    assert safety.bus_enable_attempts == 0, (
        "the bus was energised after a trip had been latched"
    )
    assert isinstance(callbacks[0][-1], SafetyTrip)


def test_clear_device_context_clears_all_parameter_consumers() -> None:
    calls = {}

    class _OptionTarget:
        def __init__(self, name) -> None:
            self.name = name

        def set_options(self, options) -> None:
            calls[self.name] = list(options)

    window = _headless_window()
    window._ui_ready = True
    window._active_device = "GaN-1"
    window.device_name_var = _FakeVar("GaN-1")
    window.param_options = {key: [(1, "one")] for key in (
        "configurations",
        "frequencies",
        "duties",
        "temperatures",
        "voltages",
    )}
    window.param_groups = {
        key: _OptionTarget(f"group:{key}") for key in window.param_options
    }
    window.parameter_editors = {
        key: _OptionTarget(f"editor:{key}") for key in window.param_options
    }
    window.tracker = SimpleNamespace(
        update_parameter_space=lambda **kwargs: calls.update(tracker=kwargs)
    )
    window.planner_tab = SimpleNamespace(
        update_parameter_options=lambda **kwargs: calls.update(planner=kwargs)
    )
    window.analytics_tab = SimpleNamespace(
        update_filter_options=lambda **kwargs: calls.update(analytics=kwargs)
    )
    window.up_next_view = SimpleNamespace(
        refresh=lambda: calls.update(up_next=True)
    )
    window.sequence = SimpleNamespace(all_configs=["old"])
    window.plot = SimpleNamespace(reset=lambda: calls.update(plot=True))
    window.last_current_label = _FakeWidget()
    window.status_bar = SimpleNamespace(
        set_message=lambda message: calls.update(status=message)
    )
    window._refresh_control_states = lambda: calls.update(controls=True)
    window._refresh_confirm_state = lambda: calls.update(confirm=True)

    MainWindow._clear_device_context(window)

    assert window._active_device == ""
    assert window.device_name_var.get() == ""
    assert all(not options for options in window.param_options.values())
    assert all(not options for options in calls["tracker"].values())
    assert all(not options for options in calls["planner"].values())
    assert all(not options for options in calls["analytics"].values())
    assert window.sequence.all_configs == []
    assert calls["up_next"] and calls["plot"]
    assert window.last_current_label.options["text"] == "Last Current: —"
    assert calls["status"] == "No device selected."


def test_remove_last_device_describes_local_database_scope(monkeypatch) -> None:
    class _Database:
        deleted = False

        def list_devices(self):
            return [] if self.deleted else ["GaN-1"]

        def delete_device(self, device_name):
            assert device_name == "GaN-1"
            self.deleted = True
            return True

    window = _headless_window()
    window.operations = SimpleNamespace(busy=False)
    window.device_name_var = _FakeVar("GaN-1")
    window.db = _Database()
    window._active_device = "GaN-1"
    calls = {}
    window._refresh_device_dropdown = lambda: calls.update(dropdown=True)
    window._clear_device_context = lambda: calls.update(cleared=True)
    prompts = []
    notices = []
    monkeypatch.setattr(
        "gan_fet.ui.main_window.messagebox.askyesno",
        lambda title, message, **_kwargs: prompts.append((title, message)) or True,
    )
    monkeypatch.setattr(
        "gan_fet.ui.main_window.messagebox.showinfo",
        lambda title, message, **_kwargs: notices.append((title, message)),
    )

    MainWindow._prompt_remove_device(window)

    prompt = prompts[0][1].lower()
    notice = notices[0][1].lower()
    assert "local database" in prompt
    assert "will not be deleted" in prompt
    assert "external files and cloud data were left unchanged" in notice
    assert calls == {"dropdown": True, "cleared": True}


def test_bulk_option_removal_uses_selected_row_indices() -> None:
    options = [(100, "100 Hz"), (200, "200 Hz"), (300, "300 Hz")]

    assert options_without_indices(options, (0, 2)) == [(200, "200 Hz")]


def test_smu_limits_do_not_expose_legacy_ramp_delay() -> None:
    assert "ramp_delay_s" not in {attr for _label, _section, attr, _cast in SmuLimitsEditor.FIELDS}


def test_ui_estop_requests_latch_before_cancelling_active_operation() -> None:
    order = []

    class _FinishedThread:
        def join(self, timeout=None) -> None:
            order.append("shutdown_join")

        def is_alive(self) -> bool:
            return False

    window = _headless_window()
    window._emergency_worker = None
    window.status_bar = SimpleNamespace(set_message=lambda _message: None)
    window._refresh_control_states = lambda: None
    window.operations = SimpleNamespace(
        active_kind="experiment",
        cancel_active=lambda: order.append("operation_cancel"),
    )
    window.sequence = SimpleNamespace(
        active=False,
        join=lambda timeout=None: True,
    )
    window.engine = SimpleNamespace(
        request_emergency_stop=lambda: (
            order.append("estop_latch_request") or _FinishedThread()
        ),
        join=lambda timeout=None: None,
        is_busy=lambda: False,
    )
    window.safety = SimpleNamespace(is_tripped=True)
    window.smu = SimpleNamespace(output_is_on=False)
    window.wavegen_controller = SimpleNamespace(outputs_armed=False)
    window.ui_dispatcher = SimpleNamespace(post=lambda *_args, **_kwargs: None)

    def run_synchronously(target, *, name):
        assert name == "emergency-stop"
        target()
        return _FinishedThread()

    window._start_worker = run_synchronously

    MainWindow._emergency_stop(window)

    assert order.index("estop_latch_request") < order.index("operation_cancel")


def test_rendered_log_row_overflow_is_bounded() -> None:
    assert rows_over_limit(4999, 5000) == 0
    assert rows_over_limit(5000, 5000) == 0
    assert rows_over_limit(5050, 5000) == 50
    with pytest.raises(ValueError):
        rows_over_limit(1, 0)
    assert single_line_text("first\r\nsecond") == r"first\r\nsecond"


def test_ramp_slider_motion_only_stages_changes() -> None:
    settings = _FakeSettings()
    editor = _headless_ramp_editor(settings)

    editor._on_v_slider("42.34")
    editor._on_freq_slider("333.4")
    editor._on_duty_slider("12.34")

    assert settings.smu.ramp_rate_v_s == 25.0
    assert settings.zvs.autotune_freq_rate_khz_s == 200.0
    assert settings.wavegen.duty_ramp_rate_pct_s == 10.0
    assert settings.save_calls == 0
    assert editor._dirty
    assert editor.apply_button.options["state"] == "normal"


def test_instrument_editor_validation_is_transport_neutral() -> None:
    assert validate_instrument_target(" GPIB0::24::INSTR ", "0") == (
        "GPIB0::24::INSTR",
        0,
    )
    assert validate_instrument_target("10.0.0.5", "5025") == (
        "10.0.0.5",
        5025,
    )
    assert validate_instrument_target(
        "prologix+serial://COM3?baud=9600&addr=24", "0"
    ) == ("prologix+serial://COM3?baud=9600&addr=24", 0)
    with pytest.raises(ValueError):
        validate_instrument_target("10.0.0.5", "0")
    with pytest.raises(ValueError):
        validate_instrument_target("unsupported://target", "0")


def test_optional_prologix_address_cannot_conflict_with_owned_addressing() -> None:
    assert validate_optional_prologix_address("", "GPIB0::24::INSTR") is None
    assert validate_optional_prologix_address("24", "192.0.2.5") == 24
    with pytest.raises(ValueError):
        validate_optional_prologix_address("24", "GPIB0::24::INSTR")
    with pytest.raises(ValueError):
        validate_optional_prologix_address(
            "24", "prologix+serial://COM3?baud=9600&addr=24"
        )


def test_instrument_apply_rolls_back_scope_address_on_save_failure(
    monkeypatch,
) -> None:
    original_instruments = {
        "HDO4054": InstrumentAddress(
            "TCPIP0::192.0.2.10::inst0::INSTR", 0
        ),
        "SDG6022X": InstrumentAddress("192.0.2.11", 5025),
        "K2410": InstrumentAddress("GPIB0::24::INSTR", 0),
    }

    class _FailingSettings:
        def __init__(self) -> None:
            self.instruments = original_instruments
            self.scope_model = "HDO4054"
            self.smu = SimpleNamespace(prologix_gpib_addr=None)

        def save(self) -> None:
            raise OSError("disk full")

    settings = _FailingSettings()
    editor = object.__new__(InstrumentConfigEditor)
    editor.settings = settings
    editor.prologix_addr_entry = _FakeVar("")
    editor.instrument_entries = {
        "HDO4054": {
            "target": _FakeVar("TCPIP0::192.0.2.50::inst0::INSTR"),
            "port": _FakeVar("0"),
        },
        "SDG6022X": {
            "target": _FakeVar("192.0.2.11"),
            "port": _FakeVar("5025"),
        },
        "K2410": {
            "target": _FakeVar("GPIB0::24::INSTR"),
            "port": _FakeVar("0"),
        },
    }
    applied = []
    editor.on_applied = lambda: applied.append(True)
    errors = []
    monkeypatch.setattr(
        "gan_fet.ui.config_tab.messagebox.showerror",
        lambda title, message, **_kwargs: errors.append((title, message)),
    )

    InstrumentConfigEditor._apply(editor)

    assert settings.instruments is original_instruments
    assert (
        settings.instruments["HDO4054"].ip
        == "TCPIP0::192.0.2.10::inst0::INSTR"
    )
    assert settings.scope_model == "HDO4054"
    assert settings.smu.prologix_gpib_addr is None
    assert not applied
    assert errors and "disk full" in errors[0][1]


def test_instrument_apply_updates_fixed_hdo_connection_without_changing_identity(
    monkeypatch,
) -> None:
    class _Settings:
        def __init__(self) -> None:
            self.instruments = {
                SCOPE_INSTRUMENT_KEY: InstrumentAddress(
                    "TCPIP0::192.0.2.10::inst0::INSTR", 0
                ),
                "SDG6022X": InstrumentAddress("192.0.2.11", 5025),
                "K2410": InstrumentAddress("GPIB0::24::INSTR", 0),
            }
            self.scope_model = "HDO4054"
            self.smu = SimpleNamespace(prologix_gpib_addr=None)
            self.save_calls = 0

        def save(self) -> None:
            self.save_calls += 1

    settings = _Settings()
    editor = object.__new__(InstrumentConfigEditor)
    editor.settings = settings
    editor.prologix_addr_entry = _FakeVar("")
    editor.instrument_entries = {
        SCOPE_INSTRUMENT_KEY: {
            "target": _FakeVar("TCPIP0::192.0.2.50::inst0::INSTR"),
            "port": _FakeVar("0"),
        },
        "SDG6022X": {
            "target": _FakeVar("192.0.2.11"),
            "port": _FakeVar("5025"),
        },
        "K2410": {
            "target": _FakeVar("GPIB0::24::INSTR"),
            "port": _FakeVar("0"),
        },
    }
    applied = []
    editor.on_applied = lambda: applied.append(True)
    monkeypatch.setattr(
        "gan_fet.ui.config_tab.messagebox.showinfo",
        lambda *_args, **_kwargs: None,
    )

    InstrumentConfigEditor._apply(editor)

    assert settings.instruments[SCOPE_INSTRUMENT_KEY] == InstrumentAddress(
        "TCPIP0::192.0.2.50::inst0::INSTR", 0
    )
    assert settings.scope_model == "HDO4054"
    assert settings.save_calls == 1
    assert applied == [True]


def test_ramp_apply_persists_one_normalized_update() -> None:
    settings = _FakeSettings()
    editor = _headless_ramp_editor(settings)
    editor.v_rate_var = _FakeVar(42.34)
    editor.freq_rate_var = _FakeVar(333.4)
    editor.duty_rate_var = _FakeVar(12.34)
    applied = []
    editor.on_applied = lambda: applied.append(True)

    editor._apply()

    assert settings.smu.ramp_rate_v_s == 42.3
    assert settings.zvs.autotune_freq_rate_khz_s == 333.0
    assert settings.wavegen.freq_ramp_rate_khz_s == 333.0
    assert settings.wavegen.duty_ramp_rate_pct_s == 12.3
    assert settings.zvs.duty_ramp_rate_pct_s == 12.3
    assert settings.save_calls == 1
    assert applied == [True]
    assert not editor._dirty
    assert editor.apply_button.options["state"] == "disabled"


# -- confirm/autotune presentation -------------------------------------------

def _presentation(**overrides):
    kwargs = dict(
        wavegen_pending=False,
        tuning_candidate_hz=None,
        tuner_busy=False,
        hardware_actions=True,
        frequency_actions=True,
    )
    kwargs.update(overrides)
    return resolve_confirm_presentation(**kwargs)


def test_pending_wavegen_outranks_an_available_tune():
    """An unapplied selection is the more urgent thing to say."""
    result = _presentation(wavegen_pending=True, tuning_candidate_hz=6_400_000.0)
    assert result.confirm_state == "pending"


def test_confirm_states_follow_the_selection():
    assert _presentation().confirm_state == "ready"
    assert _presentation(tuning_candidate_hz=6.4e6).confirm_state == "tuning"
    assert _presentation(wavegen_pending=True).confirm_state == "pending"


def test_autotune_names_a_live_bus_as_the_reason():
    result = _presentation(
        tuning_candidate_hz=6.4e6, hardware_actions=True, frequency_actions=False
    )
    assert result.autotune_enabled is False
    assert result.autotune_text == "Autotune: Bus On"


def test_autotune_is_generically_unavailable_without_a_candidate():
    result = _presentation(tuning_candidate_hz=None)
    assert result.autotune_enabled is False
    assert result.autotune_text == "Autotune Unavailable"


def test_autotune_shows_the_frequency_it_would_move_to():
    result = _presentation(tuning_candidate_hz=6_432_100.0)
    assert result.autotune_enabled is True
    assert result.autotune_text == "Autotune: 6.43 MHz"


def test_a_busy_tuner_blocks_a_second_autotune():
    assert _presentation(tuning_candidate_hz=6.4e6, tuner_busy=True).autotune_enabled is False


def test_confirm_follows_hardware_availability():
    assert _presentation(hardware_actions=False).confirm_enabled is False
    assert _presentation(hardware_actions=True).confirm_enabled is True


# -- panels apply their own slice of the control state ------------------------


def _controls(**overrides):
    """A permissive control state, so each test names only what it varies."""
    fields = {
        "edit_inputs": True,
        "local_actions": True,
        "hardware_actions": True,
        "frequency_actions": True,
        "shutdown_actions": True,
        "configuration": True,
        "reset_safety": True,
        "stop_sequence": False,
        "stop_zvs": False,
        "pause_experiment": True,
        "cancel_operation": True,
    }
    fields.update(overrides)
    return RigControlState(**fields)


def test_emergency_stop_is_never_disabled_by_any_control_state() -> None:
    """The states that disable everything else — an operation running, the
    safety latch set, the instruments offline — are exactly the ones in which
    emergency stop is needed. SmuPanel.apply_control_state must not touch it.
    """
    panel = _FakeSmuPanel()
    for controls in (
        _controls(),
        _controls(hardware_actions=False, shutdown_actions=False),
        _controls(edit_inputs=False, reset_safety=False, configuration=False),
        _controls(stop_zvs=True),
    ):
        panel.apply_control_state(controls)
    assert panel.estop_button.options == {}, (
        "emergency stop was disabled by a control-state refresh"
    )


def test_the_zvs_button_becomes_a_stop_button_while_a_search_runs() -> None:
    panel = _FakeSmuPanel()
    panel.apply_control_state(_controls(stop_zvs=True, hardware_actions=False))
    assert panel.zvs_button.options == {"state": "normal", "text": "Stop ZVS"}


def test_a_stoppable_search_stays_enabled_even_with_no_hardware_actions() -> None:
    """Stopping is not a new hardware action. A search that has become
    unstoppable because the rig went busy is a search that cannot be stopped."""
    panel = _FakeSmuPanel()
    panel.apply_control_state(_controls(stop_zvs=True, hardware_actions=False))
    assert panel.zvs_button.options["state"] == "normal"


def test_bus_off_follows_shutdown_actions_not_hardware_actions() -> None:
    """Making the rig safe must stay available when starting things is not."""
    panel = _FakeSmuPanel()
    panel.apply_control_state(
        _controls(hardware_actions=False, shutdown_actions=True)
    )
    assert panel.bus_off_button.options["state"] == "normal"


def test_reset_safety_follows_its_own_permission() -> None:
    panel = _FakeSmuPanel()
    panel.apply_control_state(_controls(reset_safety=False))
    assert panel.reset_safety_button.options["state"] == "disabled"
