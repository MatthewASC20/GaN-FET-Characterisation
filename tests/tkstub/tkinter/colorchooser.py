"""Colour picker, which a headless test must never reach."""

from __future__ import annotations

from tkinter.messagebox import _no_dialogs

askcolor = _no_dialogs
