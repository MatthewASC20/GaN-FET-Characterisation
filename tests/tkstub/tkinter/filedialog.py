"""File choosers, which a headless test must never reach."""

from __future__ import annotations

from tkinter.messagebox import _no_dialogs

askopenfilename = asksaveasfilename = askdirectory = _no_dialogs
askopenfilenames = askopenfile = asksaveasfile = _no_dialogs
