# GaN FET Characterisation

Instrument control, data acquisition and analysis for characterising GaN
power FETs in a resonant (class-E style) test rig. Cross-platform
(Windows / Linux / macOS), SQLite-backed, with automated bus-voltage
control and ZVS tuning via a Keithley 2400-series SMU.

## Hardware

| Instrument | Role | Connection |
|---|---|---|
| Keithley 2410 SMU | DC bus **source** + DC input-current meter | Direct NI-VISA at `GPIB0::24::INSTR` |
| Siglent SDG6022X | Gate drive (square wave, 6.5 Vpp, +1.9 V offset) | `10.11.13.230`, SCPI port 5025 |
| Teledyne LeCroy HDO4054 | Vds peak (P1), RMS current (P2), Isw RMS (P3), screenshots | `10.11.13.231`, LXI/VXI-11 VISA |
| Siglent SDM3055 (optional) | Independent DC bus-voltage cross-check | LAN, SCPI port 5025; disabled by default |

**SMU wiring:** the SMU sources the DC bus through the input choke and its
current readback is the DC input current — no separate series ammeter.

**GPIB connection:** A bench using direct VISA can set the Keithley target to
`GPIB0::24::INSTR`. For a Prologix bridge, use an explicit target such as
`prologix+serial://COM3?baud=9600&addr=24` or
`prologix+tcp://bridge-host:1234?addr=24`. Older settings files that used the
separate Prologix address field are folded into that URI form automatically on
load. Direct VISA is never sent `++` controller commands.

The shipped bench defaults assume the control PC is `10.11.13.10/24`, the
SDG6022X is `10.11.13.230:5025`, and the HDO4054 is `10.11.13.231`. The PC
address is operating-system configuration and is not stored as an instrument
target. An SDM3055 is used only when its endpoint is explicitly present in
`settings.json`.

The oscilloscope is fixed to the HDO4054 command set. Its shipped connection is
`TCPIP0::10.11.13.231::inst0::INSTR` with port `0`. On the scope, set
**Utilities > Utilities Setup > Remote > Control From** to **LXI** so VISA uses
VXI-11. Generic raw line TCP and direct VICP sockets are not supported; VICP
requires protocol framing that a newline-delimited socket does not provide.

Before either gate or bus output can be armed, the application queries `*IDN?`
and requires the instrument to identify as a Teledyne LeCroy HDO4054. A missing
or different identity fails closed. Configure the scope measurement table
before a run with these exact assignments:

- P1 = Vds peak
- P2 = RMS current
- P3 = Isw RMS

The software reads those preconfigured parameters; it does not redefine their
sources or measurement types at run time.

## Install

```bash
pip install -e .            # core
pip install -e ".[sheets]"  # + optional Google Sheets mirror
pip install -e ".[macos]"   # + native-looking coloured buttons on macOS
pip install -e ".[qt]"      # + PyQt6 interface preview (GPLv3 — see below)
```

The `[qt]` extra installs PyQt6, which is GPLv3-licensed; installing it places
the combined work under GPLv3.

Requires Python ≥ 3.10 with tkinter available (`python3-tk` on Debian/Ubuntu).

## Run

```bash
gan-fet                 # GUI with live bench hardware
gan-fet --simulate      # GUI with isolated virtual instruments and data
gan-fet --diagnose      # hardware connectivity self-test
gan-fet --qt            # PyQt6 interface (add --simulate to practise)
```

On macOS, you can also double-click `RUN-GAN-FET-SIMULATION.command` in the
project folder. On Windows, choose **Run Simulation Mode** from
`RUN-GAN-FET.bat`.

## Safe procedure validation

Start with `gan-fet --simulate`. A permanent purple banner must say:

> SIMULATION — VIRTUAL INSTRUMENTS — NO BENCH I/O — NOT MEASURED DATA

Simulation constructs only in-process virtual instruments; it does not probe,
open, diagnose, or shut down the real SDG6022X, HDO4054, or K2410. Google
Sheets is disabled, and settings, database, SCPI log, screenshots, reports,
and exports are all pinned below the separate `simulation/` directory.
`--simulate` cannot be combined with the live `--diagnose` mode.

To rehearse one complete test:

1. Select the device and matrix point. `SIMULATED-DUT` is created for a new
   simulation profile.
2. Leave **Tune DC Voltage before run** unchecked for a quick check, or enable
   it to rehearse the real DC voltage tune too.
3. Click **Run 6 s Procedure Validation**. This applies the virtual wavegen
   settings and executes the normal experiment engine: HDO4054 identity check,
   gate arming, SMU soft start, closed-loop Vds control, sampling, final
   readings, synthetic scope capture, database completion, and confirmed
   output shutdown.
4. Review the result in **Analytics**. On the Experiment tab, right-click the
   completed matrix point to view all final readings and sample count, plot the
   current/voltage time series, or open the simulated HDO4054 screenshot.

**Start Simulated Experiment** and **Start Simulated Sequence** run the same
virtual workflow with the selected duration and matrix. Simulation CSV and
HTML outputs include `SIMULATION` in their filenames and are visibly marked
`NOT MEASURED DATA`. The generated readings are deterministic and useful for
workflow validation; they are not a circuit-accuracy model and must not be
used as device characterisation results.

## What the app does

For each matrix point (configuration × frequency × duty × temperature ×
target Vds peak) the app:

1. soft-starts the SMU from 0 V and **closed-loop ramps the bus until the
   scope-measured Vds peak equals the selected 200/300/400 V target** —
   no manual bench-supply adjustment;
2. optionally runs a **DC voltage tune** (steps the bus voltage to the DC
   input-current minimum — the ZVS point) before sampling;
3. samples DC current/voltage on a fixed cadence into SQLite, with a live
   plot;
4. captures final readings (Vin, Iin, tuned fsw, Irms, Vds pk, Isw RMS)
   and an oscilloscope screenshot;
5. ramps the bus back to 0 V and switches the output off.

**Recall Tuned Frequency** (formerly *Autotune*) ramps the gate frequency to
the switching frequency measured in a previous run of the same point (same
temperature/config first, then other configs, then 25 °C equivalents). It
recalls a stored result; the live search is the **Tune frequency at operating
point** option in Configuration > Advanced. **Auto Sequence** runs every remaining
matrix point at the selected frequency, pausing only for chamber
temperature changes.

**Safety interlocks:** the gate configuration and output-OFF state are
verified before the bus can be enabled. Soft-start ramps, SMU
compliance-trip polling, DC-current and Vds-peak ceilings, and per-source
read-failure watchdogs remain active throughout a run (including while
paused). A safety trip or EMERGENCY STOP latches until a reset actively
confirms both gate and bus outputs are off; an unconfirmed shutdown aborts
startup or keeps the application open for recovery. Trips are recorded in
the `safety_events` table.

## Data

Live data is stored under a writable per-user data directory rather than in
the application installation. The SQLite database contains `devices`
(including per-device parameter options), append-only `runs` (one row per
attempt, preserving failed, cancelled, interrupted and repeated attempts),
`samples` (time series) and `safety_events`. Runs left active by a prior
process interruption are marked `interrupted` on the next launch.
Screenshots are PNGs under the data directory's `screenshots/` folder.

Simulation uses a separate `simulation/gan_fet_simulation.db` database,
`simulation/settings.json`, `simulation/logs/`, and
`simulation/screenshots/`. Google Sheets synchronization is disabled, so
practice runs and UI changes cannot contaminate live bench records or the live
settings profile.

Shared data directories use an exclusive heartbeat lease. A lease that
appears stale is not removed automatically (network clock skew makes that
unsafe); verify the named workstation/process is stopped before manually
removing the lock file shown in the error.

- **Reports** — *Generate Report* writes a self-contained HTML file per
  device (summary table, Iin-vs-voltage curves, frequency-tuning drift)
  under `reports/`.
- **Export** — *Export CSV* writes every append-only attempt (including failed,
  cancelled, interrupted, and repeated runs) to `<device>_runs.csv`, plus all
  associated readings to `<device>_samples.csv`. Both files carry the same
  Run ID for reliable joins and are stored under `exports/`.
- **Google Sheets (optional)** — with a service-account credentials file
  configured and *enabled* in `settings.json`, completed runs are mirrored to
  the per-device spreadsheets in the background (best effort; failures never
  block a run). The app is fully functional without it.

## Configuration

`settings.json` (created on first run) holds transport-neutral instrument
targets, the fixed HDO4054 identity discriminator, the SMU envelope (max
voltage, compliance, NPLC, and voltage ramp rate), ZVS/peak-control/safety
limits and UI state. The *Configuration* tab exposes the HDO4054 connection,
not a model selector. Legacy fixed ramp delays are migrated to the equivalent
rate. Per-device parameter option sets live in the database and follow the
device.

Default runtime locations are:

- Windows: `%LOCALAPPDATA%\GaN-FET-Characterisation`
- macOS: `~/Library/Application Support/GaN-FET-Characterisation` (logs are
  under `~/Library/Logs`)
- Linux: the applicable XDG config, data and state directories

Existing install-local settings are migrated on first use. A malformed
settings file is reported and left unchanged instead of being overwritten.
See [Architecture](docs/architecture.md) for connection URI examples, layer
boundaries, safety invariants, and extension points.

## Package layout

```
gan_fet/
  app.py            composition root + CLI
  settings.py       typed settings (JSON persisted)
  transport/        TCP / serial / VISA plus Prologix wire framing
  scpi/             serialized SCPI conversations + command events
  instruments/      rig composition + SDG6022X / HDO4054 / SDM3055 / Keithley
  core/             experiment engine, peak control, voltage tune, autotune,
                    auto-sequence, safety — no UI dependencies
  storage/          SQLite schema/queries, lease, migration, exports, reports
  sheets/           optional Drive provisioning + background Sheets mirror
  ui/               tkinter windows and widgets
tests/               headless unit and integration regressions
```
