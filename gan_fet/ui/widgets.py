"""Reusable Tk widgets, cross-platform.

macOS native buttons ignore background colours, which is why v1 depended on
tkmacosx. Here tkmacosx is optional: ColorButton falls back to tk.Button on
platforms where colours work natively.
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

try:
    from tkmacosx import Button as _ColorButtonBase  # type: ignore
    _HAVE_TKMACOSX = True
except ImportError:
    _ColorButtonBase = tk.Button
    _HAVE_TKMACOSX = False


class ColorButton(_ColorButtonBase):
    """tk.Button (or tkmacosx.Button on macOS) accepting colour kwargs."""

    def __init__(self, master=None, **kwargs):
        if not _HAVE_TKMACOSX:
            # tkmacosx-specific options that plain tk.Button rejects
            kwargs.pop("borderless", None)
            # width/height in pixels are a tkmacosx concept; plain buttons
            # size in characters — drop them and let geometry management win.
            kwargs.pop("width", None)
            kwargs.pop("height", None)
        # tkmacosx.Button recomputes its canvas size on every config() and
        # accumulates its highlight border, so the widget grows a little on
        # each restyle. Pin the constructed pixel size and re-assert it on
        # every style change.
        self._pinned_size = (
            {k: kwargs[k] for k in ("width", "height") if k in kwargs}
            if _HAVE_TKMACOSX else {}
        )
        super().__init__(master, **kwargs)

    def set_style(self, *, bg: str, fg: str, active_bg: Optional[str] = None,
                  active_fg: Optional[str] = None, **extra):
        options: Dict[str, Any] = {
            "bg": bg,
            "fg": fg,
            "activebackground": active_bg or bg,
            "activeforeground": active_fg or fg,
            **extra,
        }
        # Skip no-op updates: restyling fires on every parameter click, and
        # each config() call makes tkmacosx redraw (and re-grow) the button.
        try:
            unchanged = all(str(self.cget(k)) == str(v) for k, v in options.items())
        except tk.TclError:
            unchanged = False
        if unchanged:
            return
        options.update(self._pinned_size)
        self.config(**options)


class StatusBar(tk.Frame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.label = tk.Label(self, text="Ready.", anchor="w")
        self.label.pack(fill=tk.X)

    def set_message(self, message: str) -> None:
        self.label.config(text=message)
        self.label.update_idletasks()


def show_temporary_popup(root: tk.Misc, message: str, duration_ms: int = 1500) -> None:
    popup = tk.Toplevel(root)
    popup.title("Notification")
    popup.geometry("340x100")
    popup.attributes("-topmost", True)
    popup.resizable(False, False)
    tk.Label(popup, text=message, font=("TkDefaultFont", 13)).pack(
        expand=True, fill="both", padx=10, pady=10
    )
    # Cancel the auto-close callback if the popup dies first (e.g. app
    # shutdown), otherwise Tk logs 'invalid command name "...destroy"'.
    after_id = popup.after(duration_ms, popup.destroy)
    popup.bind("<Destroy>", lambda _e: popup.after_cancel(after_id))


class ParamButtonGroup(tk.Frame):
    """A labelled row of mutually exclusive value buttons."""

    COLS = 3
    SELECTED_BG = "#2196F3"
    UNSELECTED_BG = "#E0E0E0"

    def __init__(self, parent, options: List[Tuple[Any, str]], variable, label_text: str):
        super().__init__(parent)
        self.variable = variable
        self.options = list(options)
        self.buttons: Dict[Any, tk.Radiobutton] = {}

        self.grid_columnconfigure(1, weight=1)
        ttk.Label(self, text=label_text).grid(row=0, column=0, sticky="e", padx=(0, 12))
        self.buttons_frame = tk.Frame(self)
        self.buttons_frame.grid(row=0, column=1, sticky="ew")

        self._build_buttons()
        self.variable.trace_add("write", lambda *_: self.after(1, self._highlight))
        self.after(100, self._highlight)

    def _build_buttons(self) -> None:
        for child in self.buttons_frame.winfo_children():
            child.destroy()
        self.buttons.clear()

        uniform = f"param_btn_{id(self)}"
        for col in range(self.COLS):
            self.buttons_frame.grid_columnconfigure(
                col, weight=1, uniform=uniform, minsize=110
            )

        for idx, (value, text) in enumerate(self.options):
            button = tk.Radiobutton(
                self.buttons_frame,
                variable=self.variable,
                value=value,
                text=text,
                command=lambda v=value: self._on_click(v),
                indicatoron=False,
                selectcolor=self.SELECTED_BG,
                background=self.UNSELECTED_BG,
                foreground="black",
                activebackground="#1976D2",
                activeforeground="white",
                highlightthickness=0,
                borderwidth=1,
                relief="raised",
                padx=10,
                pady=6,
            )
            button.grid(row=idx // self.COLS, column=idx % self.COLS,
                        padx=6, pady=4, sticky="nsew")
            self.buttons[value] = button

        self._ensure_valid_selection()

    def _on_click(self, value) -> None:
        self.variable.set(value)
        self._highlight()

    def _highlight(self) -> None:
        try:
            selected = self.variable.get()
        except tk.TclError:
            return
        for value, button in self.buttons.items():
            if value == selected:
                button.config(bg=self.SELECTED_BG, fg="white",
                              selectcolor=self.SELECTED_BG, relief="sunken")
            else:
                button.config(bg=self.UNSELECTED_BG, fg="black",
                              selectcolor=self.UNSELECTED_BG, relief="raised")
        self.update_idletasks()

    def refresh(self) -> None:
        self._highlight()

    def _ensure_valid_selection(self) -> None:
        values = [v for v, _ in self.options]
        try:
            current = self.variable.get()
        except tk.TclError:
            current = None
        if values and current not in values:
            self.variable.set(values[0])

    def set_options(self, options: List[Tuple[Any, str]]) -> None:
        self.options = list(options)
        self._build_buttons()
        self.after(10, self._highlight)


def call_on_ui_thread(
    root: tk.Misc,
    func: Callable[[], Any],
    *,
    timeout: float = 300.0,
    default: Any = None,
) -> Any:
    """Run func on the Tk main thread and return its result (blocking).

    Returns `default` if the Tk loop is gone or `timeout` elapses, so a
    worker thread can never be wedged by a window that closed while it was
    waiting for an answer.

    Safe to call from worker threads; runs func directly when already on
    the main thread.
    """
    import threading

    if threading.current_thread() is threading.main_thread():
        return func()

    result: Dict[str, Any] = {}
    done = threading.Event()

    def wrapper():
        try:
            result["value"] = func()
        finally:
            done.set()

    try:
        root.after(0, wrapper)
    except RuntimeError:
        # The Tk loop is already gone; nothing will ever run the callback.
        return default

    # Never wait unboundedly: if the window closes while an engine or
    # sequence thread is waiting on a prompt, the callback is never
    # serviced and an unbounded wait would wedge that thread for good.
    if not done.wait(timeout):
        log.warning("UI call timed out after %.0fs; assuming %r", timeout, default)
        return default
    return result.get("value", default)
