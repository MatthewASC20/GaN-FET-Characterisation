"""Prompt dialogs, and the two classes matplotlib imports from here."""

from __future__ import annotations

from tkinter.messagebox import _no_dialogs

askstring = askinteger = askfloat = _no_dialogs


class Dialog:
    def __init__(self, *args, **kwargs) -> None:
        raise AssertionError("a tkinter dialog was opened in a headless test")


class SimpleDialog:
    def __init__(self, *args, **kwargs) -> None:
        raise AssertionError("a tkinter dialog was opened in a headless test")
