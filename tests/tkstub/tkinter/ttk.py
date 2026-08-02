"""Themed widget classes, inert. See ``tests/tkstub/README.md``."""

from __future__ import annotations

from tkinter import Widget


class Frame(Widget):
    pass


class LabelFrame(Widget):
    pass


class Label(Widget):
    pass


class Button(Widget):
    pass


class Entry(Widget):
    pass


class Combobox(Widget):
    pass


class Checkbutton(Widget):
    pass


class Radiobutton(Widget):
    pass


class Notebook(Widget):
    pass


class Treeview(Widget):
    pass


class Scrollbar(Widget):
    pass


class Separator(Widget):
    pass


class Progressbar(Widget):
    pass


class Scale(Widget):
    pass


class Spinbox(Widget):
    pass


class PanedWindow(Widget):
    pass


class Sizegrip(Widget):
    pass


class Style:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def configure(self, *args, **kwargs) -> None:
        pass

    def map(self, *args, **kwargs) -> None:
        pass

    def theme_use(self, *args, **kwargs) -> None:
        pass

    def theme_names(self) -> tuple[str, ...]:
        return ()
