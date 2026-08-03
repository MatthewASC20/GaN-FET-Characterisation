# CLAUDE.md

Instrument control and data acquisition for characterising GaN power FETs in a
resonant (class-E style) test rig. This software drives a **400 V bench supply
into live hardware**. Read the Safety invariants section before changing
anything under `core/`, `instruments/`, or `storage/`.

## Commands

```bash
pip install -e ".[dev]"      # core + pytest/ruff/mypy
pip install -e ".[sheets]"   # + optional Google Sheets mirror
pip install -e ".[macos]"    # + tkmacosx coloured buttons

gan-fet                      # GUI against live bench hardware
gan-fet --simulate           # GUI against virtual instruments, isolated data
gan-fet --diagnose           # hardware connectivity self-test
gan-fet --migrate            # import the legacy "Device Data" CSV tree

ruff check gan_fet scripts tests
mypy gan_fet                 # must stay clean; CI gates on it
pytest -q                    # 336 tests, all headless
```

Python ≥ 3.10 with tkinter (`python3-tk` on Debian/Ubuntu). Without it,
`tests/conftest.py` falls back to the import-only stub in `tests/tkstub/` so the
UI-logic tests still run, and prints a header saying so. **A real `tkinter`
always wins** — the fallback only triggers when the real import raises, so CI is
unaffected. The stub cannot see a wrong Tk option name, a bad geometry call or a
widget used after destruction; treat a pass under it as "the logic is right",
never as "the GUI works". See `tests/tkstub/README.md`.

CI runs lint → mypy → `compileall` → import-without-credentials → pytest on
Python 3.10 and 3.12.

## Layering

Dependencies point **down** this list. Do not introduce an upward import.

1. `app.py` — composition root and CLI. Owns process resources; closes the
   database, clients, Sheets worker and SCPI logger idempotently.
2. `ui/` — tkinter only. Translates operator actions into core operations.
3. `core/` — experiment engine, sequence, peak control, ZVS, autotune, safety.
   **No Tk dependency, ever.** This boundary is enforced by tests.
4. `instruments/` — hardware interfaces. `rig.py` is the *only* place mapping
   persisted instrument names to drivers.
5. `scpi/` — serialized SCPI conversations, publishes command events.
6. `transport/` — TCP, serial, VISA, Prologix framing. Drivers never emit
   controller (`++`) commands themselves.
7. `storage/` — SQLite schema, migrations, network lease, exports, reports.
   SQLite is the source of truth; `sheets/` is a best-effort async mirror whose
   failures must never block a run.

```
UI / CLI -> core operation -> instrument -> SCPI client -> transport
                                  |
                                  +-> event bus -> command log / file log
```

## Safety invariants

These are a design contract. Tests encode them; do not reorder E-stop,
cancellation, or output commands without extending the runtime regressions.

- Gate outputs must be configured, armed, **and read back** before the SMU can
  be enabled.
- The scope must pass an exact `*IDN?` HDO4054 identity check before either
  gate or bus output is armed. A missing or different identity **fails closed** —
  this stops a valid-looking response from another SCPI instrument being read
  as safety telemetry.
- The trip latch is set **before** any shutdown I/O begins. A concurrent
  arm/enable therefore either completes and is immediately shut down, or
  observes the latch and refuses to start.
- `SafetyMonitor._trip_generation` makes `reset_trip()` fail if a new trip
  lands mid-reset. Reset also requires no active run and independently
  confirmed shutdown.
- Emergency stop latches and records the trip *before* cancellation becomes
  visible to the experiment worker, so the terminal run state is `tripped`,
  not `cancelled`.
- Gate and bus shutdown are attempted **independently**: a failed wavegen
  transport must never skip the SMU shutdown attempt.
- Normal cancellation and pause never disable safety polling. Pause is only
  available during timed sampling.
- Every run ends with controlled shutdown followed by an emergency-off
  fallback.

Known gap: `shutdown_outputs()` sets `wavegen_ok = True` when `outputs_off()`
merely does not raise — there is no readback, unlike `arm_wavegen()`. Tightening
this is a worthwhile change.

## Threading

- All engine and sequence callbacks arrive on **worker threads** and must be
  marshalled through `UiDispatcher` (`ui/widgets.py`). Worker threads never
  invoke Tcl directly.
- `Database` uses `sqlite3.connect(check_same_thread=False)` guarded by an
  `RLock`. Keep every connection use inside that lock.
- `SafetyMonitor` has two locks: `_lock` for state, `_hardware_lock` (RLock)
  serialising anything that can energise or de-energise hardware.

## Storage

- `storage/schema.py` holds `SCHEMA_VERSION` (currently 4) and the canonical
  run-row projection. **Never duplicate a positional run projection.** To evolve
  persistence, add an idempotent structural migration and raise the version.
- `runs` is **append-only**. Each execution appends an attempt; a failed retry
  never overwrites a prior success. Terminal states: `completed`, `failed`,
  `cancelled`, `tripped`, `interrupted`, `legacy_partial`.
- Reports and the UI select the newest *successful* attempt. CSV export
  deliberately uses the separate all-attempts query.
- Shared data directories use a tokenized, fail-closed heartbeat lease. A
  stale-looking lock is **never** auto-removed — network clock skew makes that
  unsafe. Operators must verify and remove it manually.

## Simulation mode

`--simulate` runs the production engine, drivers, storage, reports and UI
against in-process virtual instruments sharing one `SimulatedRigPlant`.

- It **never** probes, opens, diagnoses or shuts down real hardware.
- `Settings.for_simulation()` pins settings, database, logs, screenshots,
  reports and exports under a separate `simulation/` tree, and disables Sheets,
  so practice runs cannot contaminate bench records.
- A permanent purple banner reads
  `SIMULATION — VIRTUAL INSTRUMENTS — NO BENCH I/O — NOT MEASURED DATA`.
  Outputs carry `SIMULATION` in their filenames.
- Mutually exclusive with `--diagnose` and `--migrate`.

Readings are deterministic and useful for workflow validation. They are **not a
circuit-accurate model** and must never be presented as characterisation results.

## Hardware boundary

| Instrument | Role | Target |
|---|---|---|
| Keithley 2410 SMU | DC bus source + input-current meter | `GPIB0::24::INSTR` |
| Siglent SDG6022X | Gate drive | `10.11.13.230:5025` |
| Teledyne LeCroy HDO4054 | Vds peak, RMS current, Isw RMS, screenshots | `TCPIP0::10.11.13.231::inst0::INSTR` |
| Siglent SDM3055 | Optional bus-voltage cross-check | LAN, off unless configured |

- The scope is **deliberately not model-selectable**. HDO4054 is the only
  accepted identity and command set.
- The driver reads an operator-configured measurement table: **P1 = Vds peak,
  P2 = RMS current, P3 = Isw RMS.** The software reads these; it does not
  redefine their sources at run time.
- Scope Remote mode must be **LXI** (VXI-11). Raw line TCP and direct VICP
  sockets are not supported — VICP needs framing a newline-delimited socket
  cannot provide.
- `smu.prologix_gpib_addr` is for legacy TCP/serial address pairs only. Leave
  blank for direct VISA. A VISA resource is never wrapped in Prologix framing.

## Conventions

- Never commit `credentials.json`. A service-account key was leaked in commit
  `c9bc6b4` and has since been revoked; it is still readable in history.
- Databases, logs, screenshots, reports, exports and `.venv/` are gitignored.
  Live data lives in a per-user data directory, never the install tree.
- A malformed `settings.json` is reported and left unchanged, never overwritten.
- Extension points: implement `transport.base.Transport` and route in
  `transport.factory`; implement an interface in `instruments/base.py` and
  register at the rig boundary; subscribe to typed events for new UI consumers.

See `docs/architecture.md` for the full rationale.
