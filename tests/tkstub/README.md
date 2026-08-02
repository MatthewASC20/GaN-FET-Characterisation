# Import-only tkinter stub

`tests/test_ui_logic.py` exercises `MainWindow` methods against instances built
with `object.__new__`. No widget is ever constructed and no Tcl call is ever
made — but importing the module still requires `tkinter`, because `MainWindow`
subclasses `tk.Tk` and matplotlib's `backend_tkagg` links against Tcl/Tk.

So on a machine without `python3-tk` those tests could not even be collected,
which is the worst place to lose a test suite: `main_window.py` is the largest
module in the project and this is its only safety net.

This package supplies just enough of `tkinter` to satisfy the imports.

## Rules it follows

- **A real `tkinter` always wins.** `conftest.py` tries the real import first
  and only falls back here if that raises `ImportError`. On CI, which installs
  `python3-tk`, this directory is never used.
- **It is loud.** When the fallback activates, pytest prints a warning header.
  A green run under the stub is not the same as a green run under real Tk, and
  the output should never let you forget which one you got.
- **Dialogs raise.** Every `messagebox`/`simpledialog` entry point raises
  `AssertionError` rather than returning a plausible value, so a test that
  accidentally reaches a dialog fails instead of quietly passing. The existing
  tests patch these by name, which is unaffected.
- **Widgets are inert classes.** They exist to be subclassed and type-checked,
  not instantiated. A test that constructs one gets an ordinary object with no
  behaviour, which will fail on first use rather than pretend to work.

## What it cannot tell you

It cannot catch a genuine misuse of the Tk API — a wrong option name, a bad
geometry call, a widget used after destruction. Those need real Tk, and CI runs
there on 3.10 and 3.12. Treat a pass here as "the logic is right", never as
"the GUI works".
