"""The Qt analytics pane over a real temporary database.

Skipped wholesale when PyQt6 is not installed; runs on the offscreen
platform plugin. These are real widgets over the real ``Database``, so a
pass means the pane displayed what storage actually wrote — the same
completed-run projection the reports use.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from gan_fet.core.models import FinalReadings, MatrixPoint  # noqa: E402
from gan_fet.storage.db import Database  # noqa: E402
from gan_fet.ui_qt.analytics import AnalyticsPane  # noqa: E402

CONFIGS = ("Dual Conduction", "Single Conduction", "Single Device")

#: Vin/Iin per configuration, chosen so the three input powers keep the
#: ordering the decomposition model requires (dual >= single conduction >=
#: single device): 20 W, 12 W and 4 W at a 400 V bus.
READINGS = {
    "Dual Conduction": (400.0, 0.050),
    "Single Conduction": (400.0, 0.030),
    "Single Device": (400.0, 0.010),
}


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "analytics.db")
    yield database
    database.close()


def _point(device: str, config: str) -> MatrixPoint:
    return MatrixPoint(
        device_name=device,
        config=config,
        frequency_hz=1_000_000,
        duty_pct=50,
        temperature_c=25,
        voltage_v=400,
    )


def _complete(database: Database, device: str, config: str) -> None:
    run_id = database.create_run(_point(device, config), 1.0)
    vin, iin = READINGS[config]
    database.complete_run(
        run_id, FinalReadings(vin=vin, iin=iin, irms=2.0)
    )


def _row_texts(pane: AnalyticsPane, row: int) -> list[str]:
    cells = []
    for column in range(pane.table.columnCount()):
        item = pane.table.item(row, column)
        cells.append(item.text() if item is not None else "")
    return cells


def test_all_three_configurations_reach_the_table_kpis_and_chart(
    qapp, db
) -> None:
    for config in CONFIGS:
        _complete(db, "QT-DUT", config)

    pane = AnalyticsPane(db)
    pane.refresh("QT-DUT")
    qapp.processEvents()

    # Filters offer exactly the completed operating point.
    assert pane.filters["frequencies"].currentText() == "1MHz"
    assert pane.filters["duties"].currentText() == "50%"
    assert pane.filters["temperatures"].currentText() == "25°C"

    # The table has a column group per configuration and one populated row.
    headers = [
        pane.table.horizontalHeaderItem(column).text()
        for column in range(pane.table.columnCount())
    ]
    for config in CONFIGS:
        assert any(config in header for header in headers)
    assert pane.table.rowCount() == 1
    cells = _row_texts(pane, 0)
    assert cells[0] == "400V"
    assert "—" not in "".join(cells)
    assert "50.00 mA" in cells  # Dual Conduction Iin, straight from storage

    # KPIs carry the computed powers: e.g. 400 V * 0.050 A = 20.00 W.
    for config, (vin, iin) in READINGS.items():
        assert f"{vin * iin:.2f} W" in pane.kpi_values[config].text()

    # The valid decomposition draws all three stacked segments.
    assert len(pane.ax.patches) == 3


def test_a_missing_configuration_renders_em_dashes_never_zero(
    qapp, db
) -> None:
    _complete(db, "QT-PARTIAL", "Dual Conduction")

    pane = AnalyticsPane(db)
    pane.refresh("QT-PARTIAL")
    qapp.processEvents()

    cells = _row_texts(pane, 0)
    # The two absent configurations show em dashes in every measure...
    assert cells[4:] == ["— V", "— mA", "— W"] * 2
    # ...and so do their KPIs, while the measured configuration keeps its
    # number. An absent run must never be displayed as zero watts.
    assert pane.kpi_values["Dual Conduction"].text() == "20.00 W"
    assert pane.kpi_values["Single Conduction"].text() == "— W"
    assert pane.kpi_values["Single Device"].text() == "— W"
    # No valid decomposition, so no bars pretend one exists.
    assert len(pane.ax.patches) == 0


def test_a_device_with_no_runs_renders_without_raising(qapp, db) -> None:
    pane = AnalyticsPane(db)
    pane.refresh("NO-SUCH-DEVICE")
    qapp.processEvents()

    assert pane.table.rowCount() == 0
    for combo in pane.filters.values():
        assert combo.count() == 0
    for config in CONFIGS:
        text = pane.kpi_values[config].text()
        assert "—" in text
        assert "0" not in text
