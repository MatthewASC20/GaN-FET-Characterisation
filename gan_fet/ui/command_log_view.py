"""Live Command Log Console & Pseudo-Command Console Widget.

Provides real-time, color-coded, anti-flooding display of SCPI traffic,
high-level pseudo-command loop states, and safety alerts.
"""

from __future__ import annotations

import queue
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional, Dict, List, Union

from gan_fet.core.events import (
    InstrumentCommandEvent,
    PseudoCommandEvent,
    SafetyTripEvent,
    bus,
)

FILTER_PSEUDO = "Pseudo-Commands / High-Level"
FILTER_HIGH_RISK = "High-Risk Only"
FILTER_RAW_SCPI = "All Commands (Raw SCPI)"

MAX_LOG_ROWS = 5000
QUEUE_BATCH_SIZE = 50
QUEUE_POLL_MS = 50

LogEvent = Union[InstrumentCommandEvent, PseudoCommandEvent, SafetyTripEvent]


def rows_over_limit(current_rows: int, maximum_rows: int) -> int:
    """Return the number of oldest rendered rows that must be removed."""

    if maximum_rows < 1:
        raise ValueError("maximum_rows must be positive")
    return max(0, current_rows - maximum_rows)


def single_line_text(value: object) -> str:
    """Escape embedded line breaks so every rendered event occupies one row."""

    return str(value).replace("\r", r"\r").replace("\n", r"\n")


class CommandLogConsole(ttk.Frame):
    """Real-time Command Log Console with risk highlighting and dynamic view filtering."""

    def __init__(
        self,
        parent: tk.Widget,
        is_simulated: bool = False,
        on_emergency_stop: Optional[Callable[[], None]] = None,
    ):
        super().__init__(parent)
        self.is_simulated = is_simulated
        self.on_emergency_stop = on_emergency_stop

        self._queue: queue.Queue[LogEvent] = queue.Queue()
        self._history: List[LogEvent] = []
        self._max_history = MAX_LOG_ROWS
        self._rendered_rows = 0
        self._auto_scroll = True
        self._filter_mode = tk.StringVar(value=FILTER_PSEUDO)
        self._search_text = tk.StringVar(value="")
        self._loop_line_map: Dict[str, str] = {}  # loop_id -> text line tag name
        self._unsubscribers: List[Callable[[], None]] = []
        self._after_id: Optional[str] = None
        self._destroyed = False

        self._build_ui()
        self._setup_tags()
        self._subscribe_events()

        self._after_id = self.after(QUEUE_POLL_MS, self._process_queue)

    def _build_ui(self) -> None:
        # Top toolbar
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=5, pady=5)

        # Simulation indicator badge
        if self.is_simulated:
            badge = tk.Label(
                toolbar,
                text="🎮 SIMULATION MODE ACTIVE",
                bg="#673ab7",
                fg="white",
                font=("Helvetica", 9, "bold"),
                padx=8,
                pady=2,
            )
            badge.pack(side="left", padx=(0, 10))

        # Filter mode dropdown
        ttk.Label(toolbar, text="View Mode:").pack(side="left", padx=(0, 5))
        mode_cb = ttk.Combobox(
            toolbar,
            textvariable=self._filter_mode,
            values=[FILTER_PSEUDO, FILTER_HIGH_RISK, FILTER_RAW_SCPI],
            state="readonly",
            width=24,
        )
        mode_cb.pack(side="left", padx=(0, 10))
        mode_cb.bind("<<ComboboxSelected>>", lambda _: self._refresh_view())

        # Search box
        ttk.Label(toolbar, text="Search:").pack(side="left", padx=(0, 5))
        search_entry = ttk.Entry(toolbar, textvariable=self._search_text, width=15)
        search_entry.pack(side="left", padx=(0, 10))
        self._search_text.trace_add("write", lambda *_: self._refresh_view())

        # Auto-scroll toggle
        self.scroll_btn = ttk.Checkbutton(
            toolbar,
            text="Auto-scroll",
            command=self._toggle_autoscroll,
        )
        self.scroll_btn.pack(side="left", padx=(0, 10))
        self.scroll_btn.state(["selected"])

        # Clear button
        ttk.Button(toolbar, text="Clear", command=self.clear_log, width=8).pack(
            side="left", padx=(0, 10)
        )

        # Emergency Stop button
        if self.on_emergency_stop:
            estop_btn = tk.Button(
                toolbar,
                text="🚨 EMERGENCY STOP",
                bg="#b71c1c",
                fg="white",
                activebackground="#7f0000",
                activeforeground="white",
                font=("Helvetica", 9, "bold"),
                command=self.on_emergency_stop,
                padx=10,
            )
            estop_btn.pack(side="right", padx=5)

        # Main Log Text Area
        text_frame = ttk.Frame(self)
        text_frame.pack(fill="both", expand=True, padx=5, pady=(0, 5))

        self.text_area = tk.Text(
            text_frame,
            wrap="none",
            font=("Consolas", 10),
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="white",
            state="disabled",
        )
        scrollbar_y = ttk.Scrollbar(
            text_frame, orient="vertical", command=self.text_area.yview
        )
        scrollbar_x = ttk.Scrollbar(
            text_frame, orient="horizontal", command=self.text_area.xview
        )
        self.text_area.configure(
            yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set
        )

        scrollbar_y.pack(side="right", fill="y")
        scrollbar_x.pack(side="bottom", fill="x")
        self.text_area.pack(side="left", fill="both", expand=True)

    def _setup_tags(self) -> None:
        """Configure syntax highlighting styles in the Tk Text widget."""
        self.text_area.tag_configure(
            "HIGH_RISK", background="#b71c1c", foreground="#ffffff", font=("Consolas", 10, "bold")
        )
        self.text_area.tag_configure(
            "MEDIUM_RISK", foreground="#ff9800", font=("Consolas", 10, "bold")
        )
        self.text_area.tag_configure("LOW_RISK", foreground="#808080")
        self.text_area.tag_configure(
            "PSEUDO_CMD", foreground="#00e5ff", font=("Consolas", 10, "bold")
        )
        self.text_area.tag_configure(
            "LOOP_ROW", background="#004d40", foreground="#a7ffeb", font=("Consolas", 10, "bold")
        )
        self.text_area.tag_configure(
            "SAFETY_TRIP", background="#d50000", foreground="#ffff00", font=("Consolas", 11, "bold")
        )

    def _subscribe_events(self) -> None:
        self._unsubscribers.extend(
            (
                bus.subscribe(
                    InstrumentCommandEvent, lambda event: self._queue.put(event)
                ),
                bus.subscribe(
                    PseudoCommandEvent, lambda event: self._queue.put(event)
                ),
                bus.subscribe(
                    SafetyTripEvent, lambda event: self._queue.put(event)
                ),
            )
        )

    def _toggle_autoscroll(self) -> None:
        self._auto_scroll = not self._auto_scroll

    def clear_log(self) -> None:
        self._history.clear()
        self._clear_loop_tags()
        self._rendered_rows = 0
        self.text_area.config(state="normal")
        self.text_area.delete("1.0", tk.END)
        self.text_area.config(state="disabled")

    def _matches_filter(self, event: LogEvent) -> bool:
        mode = self._filter_mode.get()
        search_query = self._search_text.get().strip().lower()

        # Check search text query if specified
        if search_query:
            searchable_text = ""
            if isinstance(event, SafetyTripEvent):
                searchable_text = f"{event.reason} {event.value}".lower()
            elif isinstance(event, PseudoCommandEvent):
                searchable_text = f"{event.category} {event.title} {event.detail}".lower()
            elif isinstance(event, InstrumentCommandEvent):
                searchable_text = f"{event.instrument_name} {event.command} {event.response}".lower()
            if search_query not in searchable_text:
                return False

        # Mode filtering
        if isinstance(event, SafetyTripEvent):
            return True  # Always show safety trips

        if mode == FILTER_HIGH_RISK:
            if isinstance(event, InstrumentCommandEvent):
                return event.risk_level == "HIGH"
            return False

        if mode == FILTER_PSEUDO:
            if isinstance(event, PseudoCommandEvent):
                return True
            if isinstance(event, InstrumentCommandEvent):
                return event.risk_level in ("HIGH", "MEDIUM")
            return True

        if mode == FILTER_RAW_SCPI:
            return True

        return True

    def _refresh_view(self) -> None:
        """Re-filter and re-render all stored history entries."""
        self.text_area.config(state="normal")
        self.text_area.delete("1.0", tk.END)
        self._clear_loop_tags()
        self._rendered_rows = 0

        for event in self._history:
            if self._matches_filter(event):
                self._render_and_count(event)
        self._prune_rendered_rows()

        self.text_area.config(state="disabled")
        if self._auto_scroll:
            self.text_area.see(tk.END)

    def _process_queue(self) -> None:
        """Drain queue and update UI (runs every 50ms on main thread)."""
        self._after_id = None
        if self._destroyed or not self.winfo_exists():
            return
        entries_processed = 0
        self.text_area.config(state="normal")

        while entries_processed < QUEUE_BATCH_SIZE:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            entries_processed += 1

            self._history.append(item)
            if self._matches_filter(item):
                self._render_and_count(item)

        history_overflow = rows_over_limit(len(self._history), self._max_history)
        if history_overflow:
            del self._history[:history_overflow]
        self._prune_rendered_rows()

        self.text_area.config(state="disabled")
        if self._auto_scroll and entries_processed > 0:
            self.text_area.see(tk.END)

        if not self._destroyed:
            self._after_id = self.after(QUEUE_POLL_MS, self._process_queue)

    def destroy(self) -> None:
        """Detach global listeners and cancel the recurring Tk callback."""
        if self._destroyed:
            return
        self._destroyed = True
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        for unsubscribe in self._unsubscribers:
            try:
                unsubscribe()
            except Exception:
                pass
        self._unsubscribers.clear()
        super().destroy()

    def _render_and_count(self, event: LogEvent) -> None:
        if self._render_event(event):
            self._rendered_rows += 1

    def _clear_loop_tags(self) -> None:
        for tag_name in set(self._loop_line_map.values()):
            self.text_area.tag_delete(tag_name)
        self._loop_line_map.clear()

    def _prune_rendered_rows(self) -> None:
        """Keep the Text widget bounded as well as the Python history list."""

        overflow = rows_over_limit(self._rendered_rows, self._max_history)
        if not overflow:
            return
        self.text_area.delete("1.0", f"{overflow + 1}.0")
        self._rendered_rows -= overflow

        for loop_id, tag_name in list(self._loop_line_map.items()):
            if not self.text_area.tag_ranges(tag_name):
                self.text_area.tag_delete(tag_name)
                del self._loop_line_map[loop_id]

    def _render_event(self, event: LogEvent) -> bool:
        if isinstance(event, SafetyTripEvent):
            return self._render_safety_trip(event)
        if isinstance(event, PseudoCommandEvent):
            return self._render_pseudo_event(event)
        if isinstance(event, InstrumentCommandEvent):
            return self._render_instrument_event(event)
        return False

    def _render_safety_trip(self, event: SafetyTripEvent) -> bool:
        timestamp_str = time.strftime("%H:%M:%S", time.localtime())
        reason = single_line_text(event.reason)
        value = single_line_text(event.value)
        line = f"[{timestamp_str}] 🚨 SAFETY TRIP: {reason} (Value: {value})\n"
        self.text_area.insert(tk.END, line, ("SAFETY_TRIP",))
        return True

    def _render_pseudo_event(self, event: PseudoCommandEvent) -> bool:
        timestamp_str = time.strftime("%H:%M:%S", time.localtime(event.timestamp))
        loop_id = event.loop_id
        category = single_line_text(event.category)
        title = single_line_text(event.title)
        detail = single_line_text(event.detail)

        # In-place loop row update if loop_id exists in current view
        if loop_id and loop_id in self._loop_line_map:
            tag_name = f"loop_{loop_id}"
            ranges = self.text_area.tag_ranges(tag_name)
            if ranges:
                start, end = ranges[0], ranges[1]
                iter_str = f" Iteration {event.iteration}/{event.total_iterations}" if event.total_iterations > 0 else ""
                new_line = (
                    f"[{timestamp_str}] [{category}] ⚡ "
                    f"{title}{iter_str}: {detail}\n"
                )
                self.text_area.delete(start, end)
                self.text_area.insert(start, new_line, (tag_name, "LOOP_ROW"))
                return False

        # New pseudo line
        iter_str = f" [{event.iteration}/{event.total_iterations}]" if event.total_iterations > 0 else ""
        line = f"[{timestamp_str}] [{category}] ⚡ {title}{iter_str}: {detail}\n"

        if loop_id:
            tag_name = f"loop_{loop_id}"
            self._loop_line_map[loop_id] = tag_name
            self.text_area.insert(tk.END, line, (tag_name, "LOOP_ROW"))
        else:
            self.text_area.insert(tk.END, line, ("PSEUDO_CMD",))
        return True

    def _render_instrument_event(self, event: InstrumentCommandEvent) -> bool:
        timestamp_str = time.strftime("%H:%M:%S", time.localtime(event.timestamp))
        ms = int((event.timestamp % 1) * 1000)
        mode_tag = "SIM" if event.is_simulated else "RAW"
        instrument = single_line_text(event.instrument_name)
        command = single_line_text(event.command)
        resp_str = (
            f" -> {single_line_text(event.response)}"
            if event.response is not None
            else ""
        )

        line = (
            f"[{timestamp_str}.{ms:03d}] [{mode_tag}] [{event.risk_level}] "
            f"[{instrument}] {command}{resp_str}\n"
        )

        tag = "LOW_RISK"
        if event.risk_level == "HIGH":
            tag = "HIGH_RISK"
        elif event.risk_level == "MEDIUM":
            tag = "MEDIUM_RISK"

        self.text_area.insert(tk.END, line, (tag,))
        return True
