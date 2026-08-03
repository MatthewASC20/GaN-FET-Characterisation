# Plan: splitting `ui/main_window.py`

2,633 lines, of which `MainWindow` is **97 methods and 2,498 lines**. The
largest module in the project and the last one still shaped like the
"god-object window" an earlier commit set out to remove.

## The constraint that shapes this plan

`ui/widgets.py` imports tkinter, so `tests/test_ui_logic.py` — 25 tests, the
only safety net over this file — cannot run in an environment without it.
**Refactoring 2,600 lines of UI against a suite you cannot execute is how a
working application gets broken.**

So the plan is ordered by *verifiability*, not by size:

1. work that is Tk-free and can be tested headlessly, first;
2. work that needs the GUI, in small steps with a check after each.

Every phase must leave the application runnable. After each, the gate is:

```bash
pytest -q                       # including test_ui_logic.py
gan-fet --simulate              # the window actually opens and runs a point
```

## What is actually in there

Grouping the 97 methods by responsibility:

| Responsibility | Methods | ~Lines |
|---|---|---|
| Widget construction / layout | 11 | 463 |
| Device and parameter options | 13 | 289 |
| ZVS, bus-off, emergency stop | 10 | 289 |
| Wavegen apply / autotune / confirm state | 8 | 255 |
| Experiment start / callbacks | 14 | 198 |
| Sequence control | 3 | 138 |
| Worker and operation lifecycle | 5 | 137 |
| Control-state refresh | 2 | 93 |
| Telemetry | 1 | 49 |
| Everything else | 30 | 454 |

The class is doing six unrelated jobs: building a widget tree, launching
worker threads, deciding rig command policy, holding device/parameter state,
receiving engine callbacks, and persisting UI state.

## Compatibility surface to preserve

Anything moved must keep these importable from `gan_fet.ui.main_window`, or
the move becomes a test rewrite as well as a refactor:

```python
EXPERIMENT_CONTROL_COLUMNS, EXPERIMENT_PLOT_COLUMN, EXPERIMENT_PLOT_ROWSPAN,
ExperimentRow, MainWindow, SIMULATION_VALIDATION_DURATION_MINUTES,
mode_banner_presentation
```

`tests/test_ui_logic.py` also patches `gan_fet.ui.main_window.messagebox.*`.
Code that raises dialogs must therefore either stay in this module or have its
tests updated deliberately — a silent move breaks those patches without
failing loudly.

---

## Phase 1 — Tk-free policy (no UI risk, testable here)

Extract decisions that touch no widgets. These gain real tests that run
anywhere, which is what makes the later phases safer.

**`ui/param_options.py`** — normalising, labelling and defaulting the parameter
option sets (`_normalize_options`, `_default_label`, `OPTION_KEYS`). Pure data
transformation currently buried in a window class.

**`ui/run_request.py`** — assembling a run from UI state: `_current_point`,
`_validated_current_point`, `_build_params`, `_tuning_candidate`. Returns a
value or a reason it cannot, instead of poking widgets and showing dialogs.

**Extend `ui/widgets.py` policy** — the decision half of
`_refresh_confirm_state` (pending / tuning / ready), alongside the existing
`resolve_rig_control_state`.

Roughly 250 lines out, and the first time this logic is directly testable.

## Phase 2 — Panels (mirrors the existing tab modules)

`analytics_tab.py`, `planner_tab.py` and `config_tab.py` already establish the
pattern: a class that owns its widgets and takes callbacks. Four more:

| New module | Takes over |
|---|---|
| `ui/panels/device_bar.py` | device selection, add/remove, option editors |
| `ui/panels/run_controls.py` | parameter groups, duration, run buttons |
| `ui/panels/telemetry_panel.py` | live readings display |
| `ui/panels/smu_panel.py` | ZVS / bus-off / E-STOP controls |

Each owns construction *and* refresh for its widgets, so `_build_*` and the
matching parts of `_refresh_control_states` move together. Splitting those two
apart is what created the current tangle.

Roughly 700 lines out. Highest GUI risk — do one panel per commit.

### What Phase 2 actually did

All four panels exist, one commit each. Two deviations worth recording.

**Only `DeviceBar` took its refresh with it.** `SmuPanel` and `RunControls`
expose their widgets and the window still applies `resolve_rig_control_state`
to them. The reason is the constraint at the top of this document: several
`test_ui_logic.py` tests address those widgets directly — `window.zvs_button =
_FakeWidget()` and then assert on its options — and moving the seam means
rewriting tests that cannot be executed in a headless environment. Rewriting a
test you cannot run, to match a refactor you cannot run, is not a check; it is
two unverified changes agreeing with each other. Moving that seam is the first
item of Phase 3, to be done where `pytest -q` can include `test_ui_logic.py`.

**Both conditions are now met (2026-08-02).** `tests/tkstub/` lets the 32
UI-logic tests run without `python3-tk`, and a `gan-fet --simulate` pass has
confirmed the window opens and runs a point through everything landed in
Phases 1-3. The seam is unblocked.

**About 160 lines out, not 700.** The estimate counted the parameter groups
and option editors, which already live in `ParamButtonGroup` and
`ParameterListEditor` — `_build_parameter_groups` is a ten-line loop over
them. The Tk-free extractions were the larger share of the value:
`telemetry_format.py`, `device_actions.py` and `smu_status.py` are 65 new
headless tests over decisions that previously needed a display, including the
device-removal guards, which stand in front of the most destructive action in
the UI.

Three placeholder inconsistencies fell out of the moves and were fixed, each
a case of one label contradicting the label that replaced it: the telemetry
frequency card's hardcoded `13.00 MHz`, the SMU line's `Bus:` versus
`Bus setpoint:`, and `Last Current: N/A` versus `Last Current: —`.

## Phase 3 — Operation controllers

The behaviour, separated from the widgets that trigger it:

**`ui/operations/rig_ops.py`** — apply wavegen, autotune, ZVS search, bus off,
emergency stop. Launching a worker, handling completion, reporting failure.

**`ui/operations/experiment_ops.py`** — start / pause / cancel a run, sequence
control, engine and sequence callbacks.

Both take the dispatcher and coordinator rather than the window, so the
threading contract stays explicit: worker callbacks marshal through
`UiDispatcher`, and nothing else touches Tcl.

Roughly 600 lines out.

## Phase 4 — `MainWindow` becomes composition

What remains: construct panels and controllers, wire callbacks, own the
lifecycle (`_on_closing`, worker joins). Target **under 400 lines**, doing the
one job the name implies.

### Where this actually stands (2026-08-02)

Phases 1–3 are done and verified — 385 headless tests, and a `gan-fet
--simulate` pass after each. `main_window.py` is **2,434 lines**, of which
`MainWindow` is **101 methods and 2,118 lines**. It started at 2,633 / 2,498.

That is a much smaller reduction than the line counts suggest, and the reason
is worth stating: **the value of Phases 1–3 was not lines moved, it was
untested logic becoming tested logic.** Roughly 240 tests now cover decisions
that previously could only be exercised with a display attached — the
device-removal guards, the ZVS interlock ordering, the operation-token release,
the control-state slices, the end-of-run reporting. Several of those turned up
real gaps: the guard in front of `enable_bus` whose *placement* nothing tested,
and the ZVS stop path that a careless reorder broke.

What is left, by responsibility:

| group | methods | lines |
|---|---|---|
| rig operations (wavegen, autotune, ZVS, bus off, e-stop, reset) | 17 | 518 |
| widget construction and layout | 14 | 388 |
| experiment and sequence | 20 | 374 |
| device and parameter options | 15 | 229 |
| control state and confirm presentation | 9 | 189 |
| reports, export, shutdown | 7 | 148 |
| operation lifecycle | 7 | 74 |
| other | 12 | 198 |

**The 400-line target is not reachable by extracting decisions.** Everything
separable that way has been separated. Getting under 400 means moving the rig
and experiment operations *wholesale* — 892 lines — into controller objects
holding the window's collaborators, with dialogs injected as callbacks.

That is a legitimate change, but it is a different kind of change from what
came before, and the trade should be made deliberately:

- It moves working, now partly-tested code rather than covering untested code,
  so it adds little coverage.
- It is the largest single structural move in the plan, on the module with the
  most GUI surface.
- The 700-line estimate for Phase 2 turned out to be 160. Treat 400 as a
  direction, not a number.

A smaller version worth considering on its own: `ui/operations/rig_ops.py`
already exists and holds the preconditions. Moving the five rig operations and
their completion handlers into it — the single largest and most cohesive group
— would take `MainWindow` under about 1,900 lines without touching the
experiment engine wiring.

---

## Sequencing and risk

| Phase | Lines moved | Risk | Verified by |
|---|---|---|---|
| 1 — Tk-free policy | ~250 | Low | New headless tests |
| 2 — Panels | ~700 | **High** | `test_ui_logic.py` + a simulated run |
| 3 — Controllers | ~600 | Medium | `test_ui_logic.py` + a simulated run |
| 4 — Composition | — | Low | Both |

Phase 1 is worth doing on its own even if the rest is never finished: it turns
untested logic into tested logic without touching a widget.

## What I would not do

- **One large commit.** Each phase, and each panel within Phase 2, should be
  separately revertable. A GUI regression found a week later needs a small
  diff to bisect, not a 2,000-line one.
- **Move dialog-raising code without updating its tests.** The `messagebox`
  patches are attached to this module by name and will silently stop
  intercepting.
- **Split `_refresh_control_states` from the widgets it refreshes.** Control
  policy is already Tk-free in `widgets.py`; what remains is application, and
  it belongs with the widgets it applies to.
- **Introduce a base class for panels.** Four panels with different needs do
  not justify an abstraction yet; the existing tabs manage without one.
- **Change behaviour.** This is a move, not a redesign. Anything that looks
  wrong along the way gets recorded and fixed separately, so a regression is
  never ambiguous between "the move broke it" and "the fix broke it".

## Honest caveat

I cannot run `test_ui_logic.py` in this environment, so I cannot verify any of
Phases 2–4 myself. Phase 1 I can verify completely. For the rest, the work
should be done where the tests run — or done here in small commits that you
verify one at a time.
