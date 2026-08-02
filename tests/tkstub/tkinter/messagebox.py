"""Dialogs that refuse to be answered.

Every entry point raises. A headless test that reaches a real dialog has found
a path the operator would be blocked on, and it should fail loudly rather than
receive a plausible default and carry on. Tests that need an answer patch these
by name on the module under test, which is unaffected by this.
"""

from __future__ import annotations


def _no_dialogs(*args, **kwargs):
    raise AssertionError(
        "a tkinter dialog was opened in a headless test; patch it on the "
        "module under test if the test needs an answer"
    )


showinfo = showwarning = showerror = _no_dialogs
askyesno = askokcancel = askretrycancel = askyesnocancel = _no_dialogs
askquestion = _no_dialogs

YES, NO, OK, CANCEL, RETRY, ABORT, IGNORE = (
    "yes", "no", "ok", "cancel", "retry", "abort", "ignore",
)
