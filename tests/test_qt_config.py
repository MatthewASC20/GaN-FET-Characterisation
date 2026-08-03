"""Offscreen tests for the Qt configuration pane.

Skipped wholesale when PyQt6 is not installed. Real widgets on the
offscreen platform plugin drive the real Apply slots; only the settings
object and the message boxes are doubles, so a pass means the
validate-swap-save-rollback contract actually executed.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from gan_fet.settings import (  # noqa: E402
    SCOPE_INSTRUMENT_KEY,
    InstrumentAddress,
)
from gan_fet.ui_qt import config as config_module  # noqa: E402
from gan_fet.ui_qt.config import ConfigPane  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


class _FakeSettings:
    """The Tk-test _FakeSettings pattern: instruments map + save counter."""

    def __init__(self) -> None:
        self.instruments = {
            SCOPE_INSTRUMENT_KEY: InstrumentAddress(
                "TCPIP0::192.0.2.10::inst0::INSTR", 0
            ),
            "SDG6022X": InstrumentAddress("192.0.2.11", 5025),
            "K2410": InstrumentAddress("GPIB0::24::INSTR", 0),
        }
        self.smu = SimpleNamespace(
            max_voltage_v=1100.0,
            current_compliance_a=0.1,
            nplc=1.0,
        )
        self.save_calls = 0

    def save(self) -> None:
        self.save_calls += 1


class _FailingSaveSettings(_FakeSettings):
    def save(self) -> None:
        raise OSError("disk full")


class _RecordingMessageBox:
    """Records dialogs instead of raising them: offscreen tests must never
    block on a message box."""

    calls: list[tuple[str, str, str]] = []

    @classmethod
    def critical(cls, _parent, title: str, message: str) -> None:
        cls.calls.append(("critical", title, message))

    @classmethod
    def information(cls, _parent, title: str, message: str) -> None:
        cls.calls.append(("information", title, message))


@pytest.fixture()
def dialogs(monkeypatch):
    _RecordingMessageBox.calls = []
    monkeypatch.setattr(config_module, "QMessageBox", _RecordingMessageBox)
    return _RecordingMessageBox.calls


def _pane(settings) -> tuple[ConfigPane, list[bool]]:
    applied: list[bool] = []
    pane = ConfigPane(settings, lambda: applied.append(True))
    return pane, applied


def test_valid_instrument_edit_applies_and_saves(qapp, dialogs) -> None:
    settings = _FakeSettings()
    pane, applied = _pane(settings)

    pane._instrument_edits[SCOPE_INSTRUMENT_KEY]["target"].setText(
        "TCPIP0::192.0.2.50::inst0::INSTR"
    )
    pane.apply_instruments_button.click()

    assert settings.instruments[SCOPE_INSTRUMENT_KEY] == InstrumentAddress(
        "TCPIP0::192.0.2.50::inst0::INSTR", 0
    )
    # Untouched rows are re-persisted verbatim.
    assert settings.instruments["SDG6022X"] == InstrumentAddress(
        "192.0.2.11", 5025
    )
    assert settings.save_calls == 1
    assert applied == [True]
    assert [kind for kind, _t, _m in dialogs] == ["information"]


def test_invalid_targets_report_once_and_do_not_save(qapp, dialogs) -> None:
    settings = _FakeSettings()
    original = settings.instruments
    pane, applied = _pane(settings)

    # Two independently bad rows must surface in one dialog, not the first
    # one found.
    pane._instrument_edits["SDG6022X"]["target"].setText("")
    pane._instrument_edits["K2410"]["port"].setText("not-a-port")
    pane.apply_instruments_button.click()

    assert settings.instruments is original
    assert settings.save_calls == 0
    assert applied == []
    assert len(dialogs) == 1
    kind, title, message = dialogs[0]
    assert kind == "critical" and title == "Validation Error"
    assert "SDG6022X" in message and "K2410" in message


def test_failing_save_rolls_back_previous_instruments(qapp, dialogs) -> None:
    settings = _FailingSaveSettings()
    original = settings.instruments
    pane, applied = _pane(settings)

    pane._instrument_edits[SCOPE_INSTRUMENT_KEY]["target"].setText(
        "TCPIP0::192.0.2.50::inst0::INSTR"
    )
    pane.apply_instruments_button.click()

    assert settings.instruments is original
    assert (
        settings.instruments[SCOPE_INSTRUMENT_KEY].ip
        == "TCPIP0::192.0.2.10::inst0::INSTR"
    )
    assert applied == []
    assert dialogs and dialogs[0][0] == "critical"
    assert "disk full" in dialogs[0][2]


def test_valid_limit_edit_applies_and_saves(qapp, dialogs) -> None:
    settings = _FakeSettings()
    pane, applied = _pane(settings)

    pane._limit_edits["max_voltage_v"].setText("450")
    pane._limit_edits["nplc"].setText("2.5")
    pane.apply_limits_button.click()

    assert settings.smu.max_voltage_v == 450.0
    assert settings.smu.current_compliance_a == 0.1
    assert settings.smu.nplc == 2.5
    assert settings.save_calls == 1
    assert applied == [True]
    assert dialogs == []


def test_invalid_limit_reports_and_does_not_save(qapp, dialogs) -> None:
    settings = _FakeSettings()
    pane, applied = _pane(settings)

    pane._limit_edits["current_compliance_a"].setText("-0.1")
    pane._limit_edits["nplc"].setText("abc")
    pane.apply_limits_button.click()

    assert settings.smu.current_compliance_a == 0.1
    assert settings.smu.nplc == 1.0
    assert settings.save_calls == 0
    assert applied == []
    assert len(dialogs) == 1
    assert "current_compliance_a" in dialogs[0][2]
    assert "nplc" in dialogs[0][2]


def test_failing_save_rolls_back_previous_limits(qapp, dialogs) -> None:
    settings = _FailingSaveSettings()
    pane, applied = _pane(settings)

    pane._limit_edits["max_voltage_v"].setText("450")
    pane.apply_limits_button.click()

    assert settings.smu.max_voltage_v == 1100.0
    assert applied == []
    assert dialogs and dialogs[0][0] == "critical"
    assert "disk full" in dialogs[0][2]
