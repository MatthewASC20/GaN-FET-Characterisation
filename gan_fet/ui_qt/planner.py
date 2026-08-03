"""Qt planner pane and Planned Tests queue view.

Qt renderings of the Tk :class:`~gan_fet.ui.planner_tab.PlannerTab` and
:class:`~gan_fet.ui.tracker_view.UpNextView`, driving the same toolkit-free
policy: :func:`gan_fet.core.sequence.build_matrix_plan` builds the plan,
:mod:`gan_fet.ui.plan_store` decides what the queue shows, and
:func:`gan_fet.ui.plan_table.plan_values` formats every row. Nothing is
decided here — a visible difference between the Tk and Qt planners would
mean two plans, which is the divergence the plan store exists to remove.

Selections persist through the same ``plan_selections`` tables the Tk
planner writes, so an operator can move between front-ends and find the
matrix they were working on.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Optional, Sequence

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gan_fet.core.models import sanitize_device_name
from gan_fet.core.sequence import PlanSummary, build_matrix_plan
from gan_fet.settings import Settings
from gan_fet.storage.db import Database
from gan_fet.ui.param_options import default_label
from gan_fet.ui.plan_store import NO_PLAN_HEADING, PlanStore, applied_queue
from gan_fet.ui.plan_table import (
    _TAG_STYLES,
    PLAN_COLUMNS,
    PlanRowLike,
    plan_values,
)
from gan_fet.ui.run_request import parse_positive_duration

log = logging.getLogger(__name__)

#: Same display order and titles as the Tk planner's selection boxes.
_BOX_SPECS: tuple[tuple[str, str], ...] = (
    ("Frequencies", "frequencies"),
    ("Configurations", "configurations"),
    ("Duty Cycles", "duties"),
    ("Voltages", "voltages"),
    ("Temperatures", "temperatures"),
)

_ANCHOR_ALIGNMENTS = {
    "center": Qt.AlignmentFlag.AlignCenter,
    "w": Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
}


def configure_plan_table(table: QTableWidget) -> None:
    """Apply the shared plan-table columns to a ``QTableWidget``.

    The Qt sibling of :func:`gan_fet.ui.plan_table.configure_plan_tree`:
    columns, headings and widths come from the one shared definition, so the
    Qt tables cannot drift from the Tk ones without both changing together.
    """
    table.setColumnCount(len(PLAN_COLUMNS))
    table.setHorizontalHeaderLabels(
        [heading for _id, heading, _width, _anchor in PLAN_COLUMNS]
    )
    for column, (_id, _heading, width, _anchor) in enumerate(PLAN_COLUMNS):
        table.setColumnWidth(column, width)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(
        QAbstractItemView.SelectionBehavior.SelectRows
    )
    vertical_header = table.verticalHeader()
    if vertical_header is not None:
        vertical_header.setVisible(False)


def fill_plan_table(
    table: QTableWidget,
    rows: Iterable[PlanRowLike],
    duration_minutes: Optional[float] = None,
) -> int:
    """Replace the table's contents with ``rows``. Returns how many shown.

    Cell text and the completed/pending styling both come from
    :func:`gan_fet.ui.plan_table.plan_values` and its tag styles — the same
    source the Tk trees draw from — deliberately reusing the private style
    map rather than restating its colours here, so recolouring the plan
    tables remains a one-file change.
    """
    table.setRowCount(0)
    count = 0
    for index, row in enumerate(rows, start=1):
        values, tag = plan_values(
            index, row.point, row.is_completed, duration_minutes
        )
        style = _TAG_STYLES[tag]
        foreground = QColor(style["foreground"])
        background = (
            QColor(style["background"]) if "background" in style else None
        )
        table.insertRow(count)
        for column, text in enumerate(values):
            item = QTableWidgetItem(str(text))
            item.setTextAlignment(_ANCHOR_ALIGNMENTS[PLAN_COLUMNS[column][3]])
            item.setForeground(foreground)
            if background is not None:
                item.setBackground(background)
            table.setItem(count, column, item)
        count += 1
    return count


class MultiSelectList(QWidget):
    """Titled multi-select list with All and None buttons.

    The Qt sibling of the Tk planner's ``MultiSelectBox``, keeping the same
    contract: user edits fire ``on_selection_changed`` once, programmatic
    restores fire it never.
    """

    def __init__(
        self,
        title: str,
        options: Sequence[tuple[Any, str]],
        on_selection_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__()
        self.on_selection_changed = on_selection_changed
        self.options: list[tuple[Any, str]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        header = QHBoxLayout()
        header.addWidget(QLabel(title))
        all_button = QPushButton("All")
        all_button.clicked.connect(self.select_all)
        header.addWidget(all_button)
        none_button = QPushButton("None")
        none_button.clicked.connect(self.clear_selection)
        header.addWidget(none_button)
        header.addStretch(1)
        layout.addLayout(header)

        self.list = QListWidget()
        self.list.setSelectionMode(
            QAbstractItemView.SelectionMode.MultiSelection
        )
        self.list.itemSelectionChanged.connect(self._on_select)
        layout.addWidget(self.list)

        self.set_options(options)

    def set_options(
        self,
        options: Sequence[tuple[Any, str]],
        select_all_by_default: bool = True,
    ) -> None:
        self.options = list(options)
        self.list.blockSignals(True)
        self.list.clear()
        for _value, label in self.options:
            self.list.addItem(label)
        self.list.blockSignals(False)
        if select_all_by_default:
            self.select_all()

    def select_all(self) -> None:
        """Tick everything, reporting one change rather than one per item."""
        self.list.blockSignals(True)
        for index in range(self.list.count()):
            item = self.list.item(index)
            if item is not None:
                item.setSelected(True)
        self.list.blockSignals(False)
        self._notify()

    def clear_selection(self) -> None:
        self.list.blockSignals(True)
        self.list.clearSelection()
        self.list.blockSignals(False)
        self._notify()

    def get_selected_values(self) -> list[Any]:
        return [
            value
            for index, (value, _label) in enumerate(self.options)
            if (item := self.list.item(index)) is not None
            and item.isSelected()
        ]

    def set_selected_values(self, values: Iterable[Any]) -> None:
        """Tick exactly ``values``, silently.

        Compared as strings because a restored selection comes back from
        SQLite as text, and keyed by value rather than index so an option
        removed since the selection was saved is simply not ticked. Fires no
        callback: the caller is restoring state, not editing it. The same
        contract as the Tk box.
        """
        wanted = {str(value) for value in values}
        self.list.blockSignals(True)
        for index, (value, _label) in enumerate(self.options):
            item = self.list.item(index)
            if item is not None:
                item.setSelected(str(value) in wanted)
        self.list.blockSignals(False)

    def _on_select(self) -> None:
        self._notify()

    def _notify(self) -> None:
        if self.on_selection_changed is not None:
            self.on_selection_changed()


class PlannerPane(QWidget):
    """Multi-parameter test planner: the Qt face of the Tk ``PlannerTab``.

    Building and applying are separated exactly as in Tk: the listing is
    rebuilt live on every selection change, and only "Apply Test Plan"
    commits it — through ``on_apply_plan``, so the decision about what
    applying means (stop a sequence? replace a plan?) stays with the owner
    of the plan store.
    """

    def __init__(
        self,
        db: Database,
        settings: Settings,
        get_device_name: Callable[[], str],
        on_apply_plan: Callable[[Optional[PlanSummary]], bool],
    ) -> None:
        super().__init__()
        self.db = db
        self.settings = settings
        self.get_device_name = get_device_name
        self.on_apply_plan = on_apply_plan

        self.select_boxes: dict[str, MultiSelectList] = {}
        self.current_plan: Optional[PlanSummary] = None
        #: What was last written to ``plan_selections``, so a rebuild that
        #: changed nothing costs no write.
        self._saved_selection_state: tuple = ()

        # Restores must not fire the rebuild-and-save path: the same guard
        # the Tk planner keeps while its widgets are being populated.
        self._building_ui = True
        self._build_ui()
        self._restore_selections()
        self._building_ui = False
        self.generate_plan(show_errors=False)

    def _default_options(self, key: str) -> list[tuple[Any, str]]:
        defaults: dict[str, Sequence[Any]] = {
            "configurations": self.settings.default_configurations,
            "frequencies": self.settings.default_frequencies,
            "duties": self.settings.default_duties,
            "voltages": self.settings.default_voltages,
            "temperatures": self.settings.default_temperatures,
        }
        return [(value, default_label(key, value)) for value in defaults[key]]

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        boxes = QGridLayout()
        for column, (title, key) in enumerate(_BOX_SPECS):
            box = MultiSelectList(
                title,
                self._default_options(key),
                on_selection_changed=self._on_param_selection_changed,
            )
            self.select_boxes[key] = box
            boxes.addWidget(box, 0, column)
        layout.addLayout(boxes)

        controls = QHBoxLayout()
        self.include_completed_check = QCheckBox(
            "Include Completed Runs (Re-test)"
        )
        self.include_completed_check.toggled.connect(
            lambda _checked: self._on_param_selection_changed()
        )
        controls.addWidget(self.include_completed_check)
        controls.addWidget(QLabel("Duration / test (min):"))
        self.duration_entry = QLineEdit("1.0")
        self.duration_entry.setMaximumWidth(90)
        self.duration_entry.editingFinished.connect(
            lambda: self.generate_plan(show_errors=False)
        )
        controls.addWidget(self.duration_entry)
        # No rebuild hookup: tuning is a per-run option, not a matrix axis,
        # so toggling it does not change the plan — same as Tk.
        self.tune_voltage_check = QCheckBox("Tune DC Voltage before each run")
        self.tune_voltage_check.setChecked(True)
        controls.addWidget(self.tune_voltage_check)
        controls.addStretch(1)
        self.apply_button = QPushButton("Apply Test Plan")
        self.apply_button.clicked.connect(self._apply_plan)
        controls.addWidget(self.apply_button)
        layout.addLayout(controls)

        self.summary_label = QLabel("Total Matrix Points: —")
        layout.addWidget(self.summary_label)

        self.plan_table = QTableWidget()
        configure_plan_table(self.plan_table)
        layout.addWidget(self.plan_table, stretch=1)

    def _on_param_selection_changed(self) -> None:
        if self._building_ui:
            return
        self.generate_plan(show_errors=False)

    def generate_plan(
        self, *, show_errors: bool = True
    ) -> Optional[PlanSummary]:
        """Compute the matrix test plan from the current selections."""
        device_name = self.get_device_name().strip()
        canonical_device = sanitize_device_name(device_name)
        if not device_name or canonical_device != device_name:
            self._reset_summary("No Device Selected")
            return None

        freqs = [
            int(v)
            for v in self.select_boxes["frequencies"].get_selected_values()
        ]
        configs = [
            str(v)
            for v in self.select_boxes["configurations"].get_selected_values()
        ]
        duties = [
            int(v) for v in self.select_boxes["duties"].get_selected_values()
        ]
        voltages = [
            int(v)
            for v in self.select_boxes["voltages"].get_selected_values()
        ]
        temps = [
            int(v)
            for v in self.select_boxes["temperatures"].get_selected_values()
        ]
        if not (freqs and configs and duties and voltages and temps):
            self._reset_summary("Select at least one option per parameter")
            return None

        try:
            duration = parse_positive_duration(self.duration_entry.text())
        except (TypeError, ValueError) as exc:
            self._reset_summary("Invalid duration")
            if show_errors:
                QMessageBox.critical(
                    self,
                    "Input Error",
                    "Please enter a finite, positive duration per test "
                    f"point.\n\n{exc}",
                )
            return None

        plan = build_matrix_plan(
            db=self.db,
            device_name=canonical_device,
            frequencies_hz=freqs,
            configs=configs,
            duties=duties,
            voltages=voltages,
            temperatures=temps,
            include_completed=self.include_completed_check.isChecked(),
            duration_minutes_per_point=duration,
        )
        self.current_plan = plan
        self._display_plan(plan, duration)
        return plan

    def _reset_summary(self, status_msg: str) -> None:
        self.current_plan = None
        self.summary_label.setText(
            f"Total Matrix Points: — ({status_msg})"
        )
        self.plan_table.setRowCount(0)

    def _display_plan(self, plan: PlanSummary, duration: float) -> None:
        self.summary_label.setText(
            f"Total Matrix Points: {plan.total_count}    "
            f"Already Completed: {plan.completed_count}    "
            f"Pending Execution: {plan.pending_count}    "
            f"Est. Runtime: {plan.estimated_duration_minutes:.1f} min    "
            f"Thermal Chamber Prompts: {plan.temperature_changes} "
            "transition(s)"
        )
        fill_plan_table(self.plan_table, plan.points, duration)
        self._save_selections()

    def _selection_state(self) -> tuple:
        """Everything that would be saved, in a comparable form."""
        return (
            tuple(
                (key, tuple(sorted(str(v) for v in box.get_selected_values())))
                for key, box in sorted(self.select_boxes.items())
            ),
            bool(self.include_completed_check.isChecked()),
            self.duration_entry.text(),
            bool(self.tune_voltage_check.isChecked()),
        )

    def _save_selections(self) -> None:
        """Remember which parameters are ticked, for this device.

        The selections rather than the matrix they expand to, skipped when
        nothing changed, and deliberately silent on failure — the same
        contract, and the same tables, as the Tk planner, so the two
        front-ends restore each other's work.
        """
        device = sanitize_device_name(self.get_device_name().strip())
        if not device:
            return
        state = self._selection_state()
        if state == self._saved_selection_state:
            return
        try:
            self.db.save_plan_selections(
                device,
                {
                    key: box.get_selected_values()
                    for key, box in self.select_boxes.items()
                },
                include_completed=self.include_completed_check.isChecked(),
                duration_minutes=parse_positive_duration(
                    self.duration_entry.text()
                ),
                tune_voltage=self.tune_voltage_check.isChecked(),
            )
        except Exception:
            log.exception("Could not save the planner selections")
            return
        self._saved_selection_state = state

    def _restore_selections(self) -> bool:
        """Tick what was last selected for this device. True if anything was.

        A device that has never been planned for keeps everything selected —
        the default — because "no saved selection" and "deliberately selected
        nothing" are different states, and only the second should produce an
        empty planner.
        """
        device = sanitize_device_name(self.get_device_name().strip())
        if not device:
            return False
        try:
            stored = self.db.load_plan_selections(device)
        except Exception:
            log.exception("Could not load the planner selections")
            return False
        if stored is None:
            return False
        selections, include_completed, duration, tune_voltage = stored
        for key, box in self.select_boxes.items():
            box.set_selected_values(selections.get(key, ()))
        self.include_completed_check.setChecked(include_completed)
        self.tune_voltage_check.setChecked(tune_voltage)
        self.duration_entry.setText(f"{duration:g}")
        self._saved_selection_state = self._selection_state()
        return True

    def _apply_plan(self) -> None:
        """Make the current plan the one the rig is working from."""
        plan = self.generate_plan(show_errors=True)
        self.on_apply_plan(plan)


class QueueView(QWidget):
    """The applied test plan, in the order it will run.

    The Qt face of the Tk ``UpNextView``: every outstanding point, rendered
    through the same shared row formatting as the planner's own listing,
    under the heading :mod:`gan_fet.ui.plan_store` decides. There is no
    computed fallback — an empty table means "no plan applied", and the
    heading says so in as many words.
    """

    def __init__(self, plan_store: PlanStore) -> None:
        super().__init__()
        self.plan_store = plan_store

        layout = QVBoxLayout(self)
        self.heading_label = QLabel(NO_PLAN_HEADING)
        self.heading_label.setWordWrap(True)
        layout.addWidget(self.heading_label)
        self.table = QTableWidget()
        configure_plan_table(self.table)
        layout.addWidget(self.table, stretch=1)

        self.refresh()

    def refresh(self) -> None:
        """Show the applied plan's outstanding points, or say there is none."""
        self.table.setRowCount(0)
        applied = getattr(self.plan_store, "applied", None)
        if applied is None:
            self.heading_label.setText(NO_PLAN_HEADING)
            return
        contents = applied_queue(applied)
        self.heading_label.setText(contents.heading)
        fill_plan_table(self.table, contents.rows)
