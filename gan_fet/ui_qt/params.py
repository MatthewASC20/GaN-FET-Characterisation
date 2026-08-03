"""Matrix parameter selectors: one exclusive button row per axis."""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from PyQt6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)


class ParamSelector(QWidget):
    """A labelled row of mutually exclusive value buttons.

    The first option starts selected, matching the Tk groups: an operator
    always has a complete matrix point without touching every row.
    """

    def __init__(
        self,
        title: str,
        options: Sequence[tuple[Any, str]],
        on_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__()
        self._on_changed = on_changed
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        label = QLabel(title)
        label.setMinimumWidth(110)
        layout.addWidget(label)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._values: list[Any] = []
        for index, (value, text) in enumerate(options):
            button = QPushButton(text)
            button.setCheckable(True)
            self._group.addButton(button, index)
            layout.addWidget(button)
            self._values.append(value)
            if index == 0:
                button.setChecked(True)
        layout.addStretch(1)
        self._group.idClicked.connect(self._changed)

    def _changed(self, _index: int) -> None:
        if self._on_changed is not None:
            self._on_changed()

    def value(self) -> Any:
        index = self._group.checkedId()
        return self._values[index] if 0 <= index < len(self._values) else None

    def set_value(self, value: Any) -> bool:
        for index, candidate in enumerate(self._values):
            if candidate == value:
                button = self._group.button(index)
                if button is not None:
                    button.setChecked(True)
                return True
        return False

    def set_enabled(self, enabled: bool) -> None:
        for button in self._group.buttons():
            button.setEnabled(enabled)
