# Architecture

`gan_fet` is the production package.

## Naming glossary

Operator-facing and code vocabulary follows a fixed grammar. When adding a
feature or label, pick the word from this table rather than inventing a new
one:

| Word | Reserved meaning |
|---|---|
| **Tune** | a live, measured search that moves one named knob (Tune DC Voltage, Tune Frequency) |
| **Recall** | applying a stored result from a prior run — no search (Recall Tuned Frequency) |
| **Controller** | closed-loop regulation to a setpoint, not a search (`PeakVoltageController`) |
| **ZVS** | the measured physical phenomenon only — the scope's dwell measurement (P4), its threshold, and the ZVS *point* a voltage tune lands on |

The DC voltage tune was historically called the "ZVS search" and the frequency
recall was called "Autotune". Code identifiers now follow the glossary
(`VoltageTuner`, `tune_voltage`, `tuned_voltage_v`). Three families keep old
names deliberately: persisted surfaces (`v_zvs` and `find_zvs` columns, the
`zvs` settings key) until the storage migration renames them; the `autotune`
module, which also hosts the shared `WavegenController`; and every
`zvs_dwell`/ZVS-threshold identifier, which measures actual zero-voltage
switching.

## Layering

Dependencies point down this list:

1. `app.py` is the composition root. It loads settings, owns process resources,
   builds the rig, and starts either the CLI workflow or the Tk application.
2. `ui/` translates operator actions into core operations. Worker callbacks are
   marshalled through `UiDispatcher`; worker threads never call Tcl directly.
3. `core/` owns experiment, sequence, peak-control, voltage-tune, autotune,
   and safety policy. It has no Tk dependency.
4. `instruments/` implements the hardware interfaces. `rig.py` is the only
   place that maps persisted instrument names to drivers and clients.
5. `scpi/` serializes SCPI conversations and publishes command events.
6. `transport/` owns connection parsing and wire framing for TCP, serial, VISA,
   and Prologix bridges. Instrument drivers never emit controller commands.
7. `storage/` owns the SQLite schema, migrations, network lease, exports, and
   reports. SQLite is the source of truth; `sheets/` is an optional asynchronous
   mirror.

The principal hardware path is:

```text
UI / CLI -> core operation -> hardware interface -> SCPI client -> transport
                                      |
                                      +-> event bus -> command log / file log
```

## Composition and ownership

`build_instrument_rig()` returns an `InstrumentRig` containing all drivers,
the wavegen controller, and the unique underlying clients. `_ApplicationResources`
owns the database, clients, Sheets worker, and SCPI logger and closes each one
idempotently. A partial rig build closes the clients it already created.

Simulation follows the same interfaces and composition path. Its clients share
one `SimulatedRigPlant`, and the production experiment engine, safety monitor,
drivers, storage, reports, and UI remain in the path. `Settings.for_simulation()`
pins its settings, database, logs, screenshots, reports, and exports under the
live data tree's `simulation/` directory and disables Sheets. Simulated scope
capture renders a deterministic, watermarked PNG directly from the plant and
returns before any VISA path. The CLI makes simulation mutually exclusive with
live diagnostics and legacy migration.

## Connection addresses

The historical JSON fields remain `ip` and `port` for compatibility, but `ip`
is now a transport-neutral target. Supported forms include:

- legacy TCP: `192.0.2.10` plus port `5025`;
- explicit TCP: `tcp://192.0.2.10:5025`;
- direct VISA: `GPIB0::24::INSTR` or `visa://GPIB0::24::INSTR`;
- HDO4054 LXI/VXI-11 VISA:
  `TCPIP0::10.11.13.231::inst0::INSTR` plus port `0`;
- serial: `COM3` plus baud `9600`, or `serial://COM3?baud=9600`;
- Prologix: `prologix+serial://COM3?baud=9600&addr=24` or
  `prologix+tcp://bridge-host:1234?addr=24`.

`smu.prologix_gpib_addr` exists only for legacy TCP/serial address pairs. Leave
it blank for direct VISA and explicit URIs. A VISA resource is never wrapped in
Prologix framing.

The shipped bench defaults are `10.11.13.230:5025` for the SDG6022X,
`TCPIP0::10.11.13.231::inst0::INSTR` for the HDO4054, and
`GPIB0::24::INSTR` for the K2410. The control PC's `10.11.13.10/24` address is
configured in the operating system, not in this application. The SDM3055 is an
optional cross-check and is constructed only when its endpoint is explicitly
configured.

The oscilloscope boundary is deliberately fixed to the Teledyne LeCroy
HDO4054. The default resource is
`TCPIP0::10.11.13.231::inst0::INSTR` with port `0`; the scope's **Remote** mode
must be **LXI**, which gives VISA a VXI-11 session. Generic line-oriented TCP
and direct VICP sockets are not scope transports in this application. VICP has
its own message framing and must never be treated as newline-delimited SCPI.

The HDO4054 driver reads an operator-configured measurement table:

- P1 = Vds peak;
- P2 = RMS current;
- P3 = Isw RMS.

Before arming, the driver queries `*IDN?` and requires the returned manufacturer
and model identity to match the Teledyne LeCroy HDO4054. An unavailable or
different identity aborts pre-arm validation. This prevents a valid-looking
response from another SCPI instrument from being interpreted as safety
telemetry.

Message-framed VISA resources expose an optional bounded-binary capability at
the SCPI session boundary. Scope capture holds the same session's re-entrant
transaction lock across setup, transfer, and cleanup, redacts payloads from the
command log, restores VISA timeout/termination settings, and closes a session
after a failed or oversized transfer so partial bytes cannot poison the next
query. Raw VISA `SOCKET`, serial, TCP, and Prologix streams do not advertise
this capability.

## Safety invariants

- Gate outputs must be configured, armed, and read back before the SMU can be
  enabled.
- The scope must pass its exact `*IDN?` HDO4054 identity check before either
  gate or bus output can be armed.
- Every energised phase checks SMU compliance and DC input current. Vds peak is
  also polled except while the scope exclusively owns its capture session; a
  peak check brackets that capture, while SMU safety polling continues at no
  more than 250 ms intervals.
- A trip latches before shutdown I/O begins. New energising actions are rejected
  while the latch is set.
- Emergency stop latches and records the trip before cancellation becomes
  visible to the experiment worker, so the terminal run state is `tripped`, not
  merely `cancelled`.
- Normal cancellation and pause never disable safety polling. Pause is available
  only during timed sampling.
- Reset requires no active run and independently confirmed output shutdown.
- Every run ends with controlled shutdown followed by an emergency-off fallback.

These ordering rules are part of the design contract; do not move E-stop,
cancellation, or output commands around them without extending the runtime
regression tests.

## Run and data lifecycle

Each execution appends an attempt. Prior successes are not overwritten by a
failed retry. The database marks terminal outcomes as `completed`, `failed`,
`cancelled`, `tripped`, `interrupted`, or `legacy_partial`; reports and the UI
select the newest successful attempt when one exists. CSV export deliberately
uses the separate all-attempts query and includes every attempt and sample with
a shared run ID. Samples use active elapsed time, while timestamps remain UTC
audit data.

The schema version and canonical run-row projection live in `storage/schema.py`.
Database construction performs structural migrations, rejects future schemas,
and preserves foreign-key integrity. Shared data directories use a tokenized,
fail-closed heartbeat lease; stale locks require explicit operator inspection.

## Extension points

- Add a connection type by implementing `transport.base.Transport` and routing
  an address form in `transport.factory`.
- Add an instrument model by implementing the appropriate interface in
  `instruments/base.py` and registering it at the rig composition boundary.
  The production oscilloscope is intentionally not selected dynamically: the
  only accepted scope identity and command set is the HDO4054.
- Add a UI consumer by subscribing to typed events and immediately dispatching
  any Tk work through the UI-owned dispatcher.
- Evolve persistence by adding an idempotent structural migration and raising
  `SCHEMA_VERSION`; never change positional run projections in multiple places.

## Verification

The test suite is intentionally headless. It covers transport framing and
parsing, rig composition, runtime safety/cancellation/ZVS behavior, storage
migrations and locking, Sheets degradation, packaging, and pure UI policies.
CI lints, compiles, imports without Google credentials, and runs the full suite
on Python 3.10 and 3.12.
