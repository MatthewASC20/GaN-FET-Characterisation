from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from gan_fet.core.models import MatrixPoint
from gan_fet.settings import Settings
from gan_fet.storage.db import Database

# --------------------------------------------------------------------------
# tkinter fallback
#
# test_ui_logic.py builds windows with object.__new__ and never constructs a
# widget, but importing main_window still needs tkinter: MainWindow subclasses
# tk.Tk, and matplotlib's backend_tkagg links against Tcl/Tk. Without
# python3-tk the whole file fails to collect — losing the only safety net over
# the largest module in the project.
#
# A real tkinter always wins. This only runs if the real import raises.
# --------------------------------------------------------------------------

TKINTER_IS_STUBBED = False

try:  # pragma: no cover - depends on the interpreter, not on the code
    import tkinter as _tkinter  # noqa: F401
except ImportError:  # pragma: no cover - exercised only on hosts without Tk
    sys.path.insert(0, str(Path(__file__).parent / "tkstub"))

    # matplotlib's Tk backend is a C extension and cannot be stubbed by import
    # path, so register a replacement before anything imports the real one.
    _backend = types.ModuleType("matplotlib.backends.backend_tkagg")

    class _FigureCanvasTkAgg:  # noqa: D401 - inert stand-in
        def __init__(self, *args, **kwargs) -> None:
            pass

        def get_tk_widget(self):
            return None

        def draw(self) -> None:
            pass

    class _NavigationToolbar2Tk:
        def __init__(self, *args, **kwargs) -> None:
            pass

    _backend.FigureCanvasTkAgg = _FigureCanvasTkAgg
    _backend.NavigationToolbar2Tk = _NavigationToolbar2Tk
    sys.modules["matplotlib.backends.backend_tkagg"] = _backend

    TKINTER_IS_STUBBED = True


def pytest_report_header() -> list[str]:
    """Say so, every run, when the UI tests are running against the stub.

    A pass under the stub is not a pass under real Tk — it cannot see a wrong
    option name, a bad geometry call or a widget used after destruction. The
    header exists so a green run is never mistaken for the stronger result.
    """
    if not TKINTER_IS_STUBBED:
        return []
    return [
        "tkinter: STUBBED (tests/tkstub) — UI logic is covered, real Tk "
        "behaviour is not. Install python3-tk for the full check.",
    ]


@pytest.fixture
def matrix_point() -> MatrixPoint:
    return MatrixPoint(
        device_name="TEST-FET",
        config="Single Device",
        frequency_hz=13_000_000,
        duty_pct=50,
        temperature_c=25,
        voltage_v=200,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    configured = Settings()
    configured.data_dir = str(tmp_path / "data")
    configured._settings_path = tmp_path / "settings.json"
    configured._data_base_dir = tmp_path
    return configured


@pytest.fixture
def database(tmp_path: Path):
    project = tmp_path / "project"
    db = Database(project / "gan_fet.db")
    try:
        yield db
    finally:
        db.close()
