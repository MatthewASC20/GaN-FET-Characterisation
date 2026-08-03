"""Reusable Tk widgets, cross-platform.

macOS native buttons ignore background colours, which is why v1 depended on
tkmacosx. Here tkmacosx is optional: ColorButton falls back to tk.Button on
platforms where colours work natively.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import tkinter as tk
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from functools import partial
from tkinter import ttk
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

from gan_fet.ui.run_request import (
    parse_positive_duration as _parse_positive_duration,
)

try:
    from tkmacosx import Button as _ColorButtonBase  # type: ignore
    _HAVE_TKMACOSX = True
except ImportError:
    _ColorButtonBase = tk.Button
    _HAVE_TKMACOSX = False


log = logging.getLogger(__name__)


class UiDispatcherClosed(RuntimeError):
    """Raised when work is submitted after the Tk dispatcher has closed."""


class UiDispatcher:
    """Thread-safe queue drained exclusively by the Tk owner thread.

    Worker threads only touch the Python queue; they never invoke ``after`` or
    any other Tcl command themselves.
    """

    def __init__(self, root: tk.Misc, *, poll_ms: int = 20, max_batch: int = 200):
        self.root = root
        self.poll_ms = poll_ms
        self.max_batch = max_batch
        self._owner_ident = threading.get_ident()
        self._queue: "queue.Queue[tuple[Callable[..., Any], tuple, dict, Future[Any]]]" = (
            queue.Queue()
        )
        self._closed = threading.Event()
        self._after_id: Optional[str] = self.root.after(self.poll_ms, self._drain)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def post(self, func: Callable[..., Any], *args, **kwargs) -> Future[Any]:
        future: Future[Any] = Future()
        if self.closed:
            future.set_exception(UiDispatcherClosed("UI dispatcher is closed"))
            return future
        if threading.get_ident() == self._owner_ident:
            self._execute(func, args, kwargs, future)
        else:
            self._queue.put((func, args, kwargs, future))
        return future

    def call(
        self,
        func: Callable[[], Any],
        *,
        timeout: Optional[float] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Any:
        """Run ``func`` on Tk and block until the operator/UI resolves it."""
        if threading.get_ident() == self._owner_ident:
            return func()
        future = self.post(func)
        started = time.monotonic()
        while True:
            if cancel_event is not None and cancel_event.is_set():
                future.cancel()
                return None
            if self.closed:
                future.cancel()
                return None
            remaining = None
            if timeout is not None:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    future.cancel()
                    return None
            try:
                return future.result(timeout=min(0.1, remaining) if remaining else 0.1)
            except FutureTimeout:
                continue

    def _execute(self, func, args, kwargs, future: Future[Any]) -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(func(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
            log.exception("UI-dispatched callback failed")

    def _drain(self) -> None:
        self._after_id = None
        if self.closed:
            return
        for _ in range(self.max_batch):
            try:
                func, args, kwargs, future = self._queue.get_nowait()
            except queue.Empty:
                break
            self._execute(func, args, kwargs, future)
        if not self.closed:
            self._after_id = self.root.after(self.poll_ms, self._drain)

    def close(self) -> None:
        """Close on the Tk owner thread and unblock all waiting workers."""
        if self._closed.is_set():
            return
        self._closed.set()
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except (tk.TclError, AttributeError):
                pass
            self._after_id = None
        while True:
            try:
                _func, _args, _kwargs, future = self._queue.get_nowait()
            except queue.Empty:
                break
            if not future.done():
                future.set_exception(UiDispatcherClosed("UI dispatcher is closed"))


@dataclass(frozen=True)
class OperationToken:
    kind: str
    cancel_event: threading.Event = field(default_factory=threading.Event)


class OperationCoordinator:
    """One exclusive lease for every operation that can touch the rig."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: Optional[OperationToken] = None

    @property
    def active(self) -> Optional[OperationToken]:
        with self._lock:
            return self._active

    @property
    def active_kind(self) -> Optional[str]:
        token = self.active
        return token.kind if token else None

    @property
    def busy(self) -> bool:
        return self.active is not None

    def try_begin(self, kind: str) -> Optional[OperationToken]:
        with self._lock:
            if self._active is not None:
                return None
            token = OperationToken(kind)
            self._active = token
            return token

    def finish(self, token_or_kind: OperationToken | str) -> bool:
        with self._lock:
            if self._active is None:
                return False
            matches = (
                self._active is token_or_kind
                if isinstance(token_or_kind, OperationToken)
                else self._active.kind == token_or_kind
            )
            if not matches:
                return False
            self._active = None
            return True

    def cancel_active(self) -> None:
        token = self.active
        if token is not None:
            token.cancel_event.set()

    def begin_stopping(self) -> OperationToken:
        with self._lock:
            if self._active is not None:
                self._active.cancel_event.set()
            token = OperationToken("stopping")
            token.cancel_event.set()
            self._active = token
            return token


@dataclass(frozen=True)
class ConfirmPresentation:
    """How the confirm and autotune buttons should currently read.

    Kept separate from the widgets so the reasoning — which is the part that
    has been got wrong before — can be checked without a display. The window
    only applies colours and text.
    """

    confirm_state: Literal["pending", "tuning", "ready"]
    confirm_enabled: bool
    autotune_enabled: bool
    autotune_text: str


def resolve_confirm_presentation(
    *,
    wavegen_pending: bool,
    tuning_candidate_hz: Optional[float],
    tuner_busy: bool,
    hardware_actions: bool,
    frequency_actions: bool,
) -> ConfirmPresentation:
    """Resolve the confirm/autotune presentation for one state snapshot."""
    if wavegen_pending:
        confirm_state: Literal["pending", "tuning", "ready"] = "pending"
    elif tuning_candidate_hz is not None:
        confirm_state = "tuning"
    else:
        confirm_state = "ready"

    autotune_enabled = (
        tuning_candidate_hz is not None and not tuner_busy and frequency_actions
    )
    if autotune_enabled and tuning_candidate_hz is not None:
        autotune_text = (
            f"Recall Tuned Frequency: {tuning_candidate_hz / 1e6:.2f} MHz"
        )
    elif hardware_actions and not frequency_actions:
        # Name the cause: a live bus is something the operator can fix, unlike
        # simply having no prior run to tune towards.
        autotune_text = "Recall Tuned Frequency (Bus On)"
    else:
        autotune_text = "No Tuned Frequency Stored"

    return ConfirmPresentation(
        confirm_state=confirm_state,
        confirm_enabled=hardware_actions,
        autotune_enabled=autotune_enabled,
        autotune_text=autotune_text,
    )


@dataclass(frozen=True)
class RigControlState:
    """Resolved UI permissions for one snapshot of rig/application state.

    Keeping this policy independent of Tk makes it straightforward to verify
    that offline mode cannot accidentally re-enable an energising action when
    another callback refreshes the window.
    """

    edit_inputs: bool
    local_actions: bool
    hardware_actions: bool
    #: Actions that move the gate frequency. Denied while the bus is
    #: energised: changing frequency shifts the resonant operating point, and
    #: therefore Vds peak, so it is only unconditionally safe with the bus off.
    frequency_actions: bool
    shutdown_actions: bool
    configuration: bool
    reset_safety: bool
    stop_sequence: bool
    stop_zvs: bool
    pause_experiment: bool
    cancel_operation: bool


def resolve_rig_control_state(
    *,
    active_kind: Optional[str],
    closing: bool,
    safety_tripped: bool,
    hardware_offline: bool,
    engine_running: bool,
    bus_energised: bool = False,
) -> RigControlState:
    """Return control permissions without consulting or mutating Tk widgets."""

    idle = active_kind is None and not closing
    hardware_reachable = not hardware_offline
    hardware_actions = idle and hardware_reachable and not safety_tripped
    sequence_active = active_kind == "sequence"
    zvs_active = active_kind == "zvs"

    return RigControlState(
        edit_inputs=idle,
        local_actions=idle,
        hardware_actions=hardware_actions,
        # A standalone frequency move has no closed-loop peak control behind
        # it, so it is confined to a de-energised bus. The in-run frequency
        # search is a different path: it holds Vds peak on target throughout.
        frequency_actions=hardware_actions and not bus_energised,
        # De-energising actions may remain available during a safety trip, but
        # not when startup established that the hardware transport is offline.
        shutdown_actions=idle and hardware_reachable,
        configuration=idle,
        reset_safety=idle and hardware_reachable and safety_tripped,
        stop_sequence=sequence_active and not closing,
        stop_zvs=zvs_active and not closing,
        pause_experiment=engine_running and not closing,
        cancel_operation=(engine_running or sequence_active or zvs_active)
        and not closing,
    )


# Re-exported so existing importers keep working. The parsing itself is pure
# and lives in ui/run_request.py, which does not import tkinter.
parse_positive_duration = _parse_positive_duration


def set_widget_enabled(widget, enabled: bool) -> None:
    """Enable or disable one control, tolerating one that is not there.

    Control-state refresh runs on every operation transition, including during
    teardown and before the whole window has been built. A missing or
    already-destroyed widget is an ordinary case on those paths, not a fault,
    and must not stop the rest of the controls being updated — a half-applied
    refresh could leave an energising action enabled when it should not be.
    """
    if widget is None:
        return
    try:
        widget.config(state="normal" if enabled else "disabled")
    except (tk.TclError, AttributeError):
        pass


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
        self._pinned_size: dict[str, Any] = (
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

    set_text = set_message


def show_temporary_popup(root: tk.Misc, message: str, duration_ms: int = 1500) -> None:
    popup = tk.Toplevel(root)
    popup.title("Notification")
    popup.geometry("340x100")
    popup.attributes("-topmost", True)
    popup.resizable(False, False)
    tk.Label(popup, text=message, font=("TkDefaultFont", 13)).pack(
        expand=True, fill="both", padx=10, pady=10
    )
    popup.after(duration_ms, popup.destroy)


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
                command=partial(self._on_click, value),
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
        if not self.options:
            self.grid_remove()
        else:
            self.grid()
        self.after(10, self._highlight)

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable every choice without changing the selection."""
        desired: Literal["normal", "disabled"] = (
            "normal" if enabled else "disabled"
        )
        for button in self.buttons.values():
            button.config(state=desired)


def call_on_ui_thread(
    root: tk.Misc,
    func: Callable[[], Any],
    timeout: Optional[float] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Any:
    """Run func on the Tk main thread and return its result (blocking).

    Safe to call from worker threads; runs func directly when already on
    the main thread.
    """
    if threading.current_thread() is threading.main_thread():
        return func()
    dispatcher = getattr(root, "ui_dispatcher", None)
    if not isinstance(dispatcher, UiDispatcher):
        raise RuntimeError("Tk root has no UiDispatcher; refusing a cross-thread Tcl call")
    return dispatcher.call(func, timeout=timeout, cancel_event=cancel_event)
