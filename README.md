# GaN FET Characterisation

Instrument control, data acquisition and analysis for characterising GaN
power FETs in a resonant (class-E style) test rig. Cross-platform
(Windows / Linux / macOS), SQLite-backed, with automated bus-voltage
control and ZVS tuning via a Keithley 2400-series SMU.

## Hardware

| Instrument | Role | Connection |
|---|---|---|
| Keithley 2400-series SMU (e.g. 2410) | DC bus **source** + DC input-current meter | GPIB/serial-to-LAN bridge, SCPI over TCP |
| Siglent SDG6022X | Gate drive (square wave, 6.5 Vpp, +1.9 V offset) | LAN, SCPI port 5025 |
| Tektronix MSO44 | Vds peak (MEAS6), RMS current (MEAS7), Isw RMS (MEAS9), screenshots | LAN, port 4000 / pyvisa |
| Siglent SDM3055 | Independent DC bus-voltage cross-check | LAN, SCPI port 5025 |

**SMU wiring:** the SMU sources the DC bus through the input choke and its
current readback is the DC input current — no separate series ammeter.

**LAN bridge:** a transparent bridge needs no extra configuration. For a
Prologix GPIB-ETHERNET bridge (default port 1234), set the instrument's
GPIB address in *Configuration → SMU / Safety Limits → Prologix GPIB addr*;
the driver sends the `++` framing commands automatically.

## Install

```bash
pip install -e .            # core
pip install -e ".[sheets]"  # + optional Google Sheets mirror
pip install -e ".[macos]"   # + native-looking coloured buttons on macOS
```

Requires Python ≥ 3.10 with tkinter available (`python3-tk` on Debian/Ubuntu).

## Run

```bash
gan-fet               # GUI
gan-fet --migrate     # one-time import of a legacy "Device Data" CSV tree
```

On first launch with an empty database, the app offers the legacy import
automatically.

## What the app does

For each matrix point (configuration × frequency × duty × temperature ×
target Vds peak) the app:

1. soft-starts the SMU from 0 V and **closed-loop ramps the bus until the
   scope-measured Vds peak equals the selected 200/300/400 V target** —
   no manual bench-supply adjustment;
2. optionally runs a **ZVS search** (steps the bus voltage to find the DC
   input-current minimum) before sampling;
3. samples DC current/voltage on a fixed cadence into SQLite, with a live
   plot;
4. captures final readings (Vin, Iin, tuned fsw, Irms, Vds pk, Isw RMS)
   and an oscilloscope screenshot;
5. ramps the bus back to 0 V and switches the output off.

**Autotune** ramps the gate frequency to the switching frequency measured in
a previous run of the same point (same temperature/config first, then other
configs, then 25 °C equivalents). **Auto Sequence** runs every remaining
matrix point at the selected frequency, pausing only for chamber
temperature changes.

**Safety interlocks:** soft-start ramps everywhere, SMU compliance-trip
polling, DC-current and Vds-peak ceilings, a watchdog on consecutive failed
reads, and an EMERGENCY STOP button. Any trip switches the SMU output and
the wavegen gates off and is recorded in the `safety_events` table.

## Data

Everything lives in one SQLite file (`gan_fet.db`): `devices` (incl.
per-device parameter options), `runs` (one row per matrix point, with final
readings, bus voltage and V_ZVS), `samples` (time series) and
`safety_events`. Screenshots are PNGs under `screenshots/`.

- **Reports** — *Generate Report* writes a self-contained HTML file per
  device (summary table, Iin-vs-voltage curves, frequency-tuning drift)
  under `reports/`.
- **Export** — *Export CSV* writes `<device>_runs.csv` and
  `<device>_samples.csv` under `exports/`.
- **Google Sheets (optional)** — with `credentials.json` present and
  *enabled* in `settings.json`, completed runs are mirrored to the
  per-device spreadsheets in the background (best effort; failures never
  block a run). The app is fully functional without it.

## Configuration

`settings.json` (created on first run) holds instrument addresses, SMU
envelope (max voltage, compliance, NPLC), ZVS/peak-control/safety limits
and UI state; all editable in the *Configuration* tab. Per-device parameter
option sets live in the database and follow the device.

## Package layout

```
gan_fet/
  app.py            composition root + CLI
  settings.py       typed settings (JSON persisted)
  instruments/      ScpiTcpClient + SDG6022X / MSO44 / SDM3055 / Keithley 2400
  core/             experiment engine, peak control, ZVS, autotune,
                    auto-sequence, safety — no UI dependencies
  storage/          SQLite schema/queries, legacy migration, exports, reports
  sheets/           optional Drive provisioning + background Sheets mirror
  ui/               tkinter windows and widgets
```
