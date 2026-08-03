"""Enough of ``tkinter`` to import the UI, and nothing more.

Used only when the real module is missing — see ``tests/tkstub/README.md``.
The widget classes exist to be subclassed and imported, not instantiated: the
tests they support build windows with ``object.__new__`` and never touch Tcl.
"""

from __future__ import annotations

TkVersion = 8.6
TclVersion = 8.6


class TclError(Exception):
    """Raised by real Tk for bad options and dead widgets."""


class Misc:
    """Base of the widget hierarchy.

    ``__getattr__`` raises rather than returning a mock, so an attribute the
    real class would not have fails here too instead of silently working.
    """

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __getattr__(self, name: str):
        raise AttributeError(name)


class Wm:
    pass


class Widget(Misc):
    pass


class BaseWidget(Widget):
    pass


class Tk(Misc, Wm):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__()


class Toplevel(Widget):
    pass


class Frame(Widget):
    pass


class Label(Widget):
    pass


class Button(Widget):
    pass


class Canvas(Widget):
    pass


class Entry(Widget):
    pass


class Text(Widget):
    pass


class Listbox(Widget):
    pass


class Scrollbar(Widget):
    pass


class Menu(Widget):
    pass


class Menubutton(Widget):
    pass


class Message(Widget):
    pass


class OptionMenu(Widget):
    pass


class Checkbutton(Widget):
    pass


class Radiobutton(Widget):
    pass


class Scale(Widget):
    pass


class Spinbox(Widget):
    pass


class PanedWindow(Widget):
    pass


class PhotoImage:
    def __init__(self, *args, **kwargs) -> None:
        pass


class BitmapImage(PhotoImage):
    pass


class Event:
    pass


class Variable:
    """A plain value holder. Traces are accepted and never fire.

    Nothing here schedules work on a Tk event loop, so a trace could only fire
    if something called it directly — which would be testing the stub.
    """

    def __init__(self, master=None, value=None, name=None) -> None:
        self._value = value

    def get(self):
        return self._value

    def set(self, value) -> None:
        self._value = value

    def trace_add(self, *args, **kwargs) -> str:
        return ""

    def trace_remove(self, *args, **kwargs) -> None:
        pass


class StringVar(Variable):
    def __init__(self, master=None, value="", name=None) -> None:
        super().__init__(master, value, name)


class IntVar(Variable):
    def __init__(self, master=None, value=0, name=None) -> None:
        super().__init__(master, value, name)


class DoubleVar(Variable):
    def __init__(self, master=None, value=0.0, name=None) -> None:
        super().__init__(master, value, name)


class BooleanVar(Variable):
    def __init__(self, master=None, value=False, name=None) -> None:
        super().__init__(master, value, name)


def mainloop(*args, **kwargs) -> None:
    raise AssertionError("mainloop() started in a headless test")


# Geometry and option constants, values as in real tkinter.
END = "end"
INSERT = "insert"
ANCHOR = "anchor"
ALL = "all"
N, S, E, W = "n", "s", "e", "w"
NE, NW, SE, SW = "ne", "nw", "se", "sw"
NS, EW, NSEW, CENTER = "ns", "ew", "nsew", "center"
LEFT, RIGHT, TOP, BOTTOM = "left", "right", "top", "bottom"
BOTH, X, Y, NONE = "both", "x", "y", "none"
HORIZONTAL, VERTICAL = "horizontal", "vertical"
NORMAL, DISABLED, ACTIVE = "normal", "disabled", "active"
SINGLE, BROWSE, MULTIPLE, EXTENDED = "single", "browse", "multiple", "extended"
RAISED, SUNKEN, FLAT, RIDGE, GROOVE, SOLID = (
    "raised",
    "sunken",
    "flat",
    "ridge",
    "groove",
    "solid",
)
