# Plan: joint frequency / bus-voltage optimisation

Companion to `frequency-tuning-problem.md`.

## Status

| Question | Answer | Source |
|---|---|---|
| Objective | `P_in = V_bus × I_dc`, not `I_dc` | `dP_in/df = I_dc · dV_bus/df ≠ 0` at the I_dc minimum |
| Is the objective unimodal in `f`? | **No** — multiple distinct minima | Bench observation |
| Hysteresis present? | Yes, but **judged small enough to ignore** | Bench observation; see below |
| Search framing | Nested: frequency outer, peak control inner | — |
| Voltage-only ZVS sweep | Retained, behind a Config checkbox | Requirement |
| Manual reference | 0.1 MHz granularity finds the minimum | Existing practice |

## The simplifying assumption

Returning frequency or bus voltage to a starting value *sometimes* produces a
different result, so the plant is not strictly single-valued. The working
assumption is that the effect is **small enough to treat as measurement scatter
rather than structure**.

That assumption buys a great deal: it restores a memoryless inner loop, so the
bus can be converged to the target peak at every frequency, and the search
collapses from a six-sweep directional mapping exercise to a single sweep.

It is validated, not assumed, by one cheap Phase 1 test (§ Phase 1). If the
effect turns out to be large, the design reverts to path-aware mapping — but
that decision is made on a measurement, not a guess.

**What survives regardless:** multiple minima are real, so the search must
cover the window rather than descend locally. Coverage does that work, not the
hysteresis machinery.

## Safety model

A branch transition, if one occurs, completes in microseconds against a 250 ms
polling interval. Reacting faster is a race that cannot be won, so bound
reachability instead:

```
V_bus_cap = V_ceiling_safe / A_max_est
```

`A_max_est` is a conservative bound on tank voltage gain, measured in Phase 1;
`V_ceiling_safe` sits below the 450 V interlock (e.g. 430 V). Applied to the
SMU maximum-voltage setting for the duration of the search, this is a static
guarantee rather than a reaction.

Supporting notes:

- The 0.1 A SMU compliance limit fires before the voltage ceiling and is the
  *de facto* fast detector. Treat that deliberately.
- The 10 V overshoot allowance applies to smooth frequency stepping.
- **Leave the 450 V interlock alone.**

---

## The algorithm: nested coarse-then-fine

### Structure

Frequency is the outer variable. At each frequency the existing peak controller
drives the bus until scope-measured Vds peak equals the target, then `P_in` is
read. Every evaluated point is therefore measured on-target — nothing is
interpolated, and no separate confirmation stage is needed.

Multiple minima are handled by sweeping the whole window at the granularity
that is known to work manually (0.1 MHz), rather than by any local descent.

### Stage 0 — Window selection

The window must be derived, not assumed. A tank whose real resonance falls
outside it cannot be found by any amount of searching inside it.

| Situation | Action |
|---|---|
| Warm start available (history, or previous voltage point) | Narrow window around it; skip the survey |
| No history for this device / bank | **Low-amplitude survey** (below), set window from the measured resonance |
| Coarse sweep later lands on a window edge | Escalate — widen and re-survey |

**Low-amplitude resonance survey.** Run at 15–20 % of target Vds peak, where
Coss varies little and the tank is near-linear, so the resonance reads cleanly.
No bus convergence per point — set frequency, settle, read — and low enough
energy to sweep far wider than the working search:

```
from home, canonical approach to a low bus level
for f in ±50 % of nominal at 0.2 MHz steps:
    ramp frequency to f; settle; read Vds_peak
estimate f₀ from the gain peak, and A_lin = max(Vds_peak) / V_bus
```

`A_lin` also seeds `A_max_est` for the safety cap.

**Apply the amplitude offset.** The low-amplitude resonance sits *below* the
working-amplitude one — Coss is larger at low Vds, so the tank is slower. Bias
the working window upward from the survey peak rather than centring on it. The
size of that bias is sized from Phase 0's drift analysis across the
200/300/400 V points.

**Report, do not silently compensate.** If the measured resonance is more than
~20 % from nominal, the L/C bank did not land where it was designed to. The
software should characterise where the resonance actually is *and say so* — the
run is no longer testing the operating point the bank was built for. Note also
that a ±50 % survey at 13 MHz overlaps the 6 MHz matrix cell; acceptable for a
survey, not for a recorded run.

### Stage 1 — Coarse sweep

```
from home, canonical approach to the first frequency
for f in window at 0.1 MHz steps (ascending):
    ramp frequency to f
    converge bus to target Vds peak      # bounded secant, soft band clamp
    read V_bus, I_dc  →  P_in = V_bus × I_dc
    run anomaly detector
    record (f, V_bus, achieved peak, I_dc, P_in, direction)
```

**Bounded secant inner loop.** Local gain `Ĝ = ΔVds_peak / ΔV_bus` is estimated
from the previous two readings; step size is clamped within the soft band.
Falls back to proportional control when `|Ĝ|` is small or the estimate is
noisy. Typically converges in 2–3 steps against 6–10 for fixed-gain control.

**Adaptive frequency step.** From measured `dVds_peak/df`, widen the step where
the response is flat and shrink it where it steepens, so the predicted peak
change stays inside the soft band. 0.1 MHz is the default, not a fixed value.

**Anomaly detector.** Flag any point where `|ΔVds_peak|` or `|ΔI_dc|` exceeds
N× the value predicted from the local slope. Retained as a data-quality and
safety flag even though the design no longer routes around hysteresis — it is
nearly free and is how you would find out the simplifying assumption was wrong.

### Stage 2 — Minimum selection and edge check

Identify **all** local minima in `P_in(f)` across the sweep, not just the best.
Record them; they are a device property worth keeping. Select the global
minimum for refinement.

**Edge check — the window may have clipped the answer.** Two signatures:

- The selected minimum sits at `f_lo` or `f_hi` with `P_in` still falling.
- Peak control could not reach target *anywhere* in the window within
  `V_bus_cap`. Across the whole window this is not a degenerate point but a
  diagnosis: the tank gain is too low everywhere, so resonance is far outside.

Either triggers escalation — widen the window and re-run the low-amplitude
survey. If a widened survey still shows no interior minimum, stop and report
rather than searching indefinitely; at that point the bank, not the software,
is the thing to change.

### Stage 3 — Fine refinement

Re-sweep ±0.1 MHz around the selected minimum at ~0.02 MHz, same inner loop.
Take the lowest measured `P_in`.

### Stage 4 — Run

Proceed to the normal sampling run at the refined point. Propagate the result
as the warm start for the next voltage point and the next temperature.

### Cost

| Per coarse point | |
|---|---|
| Frequency ramp, 0.1 MHz @ 200 kHz/s | 0.50 s |
| Bus convergence (2–3 secant steps) | ~1.8 s |
| Read Vds peak + I_dc | 0.30 s |
| **Total** | **~2.6 s** |

A low-amplitude survey point costs less — no bus convergence — at ~1.8 s.

| | Points | Time |
|---|---|---|
| Low-amplitude survey, ±50 % @ 0.2 MHz | 30 | 54 s |
| Coarse, ±20 % window @ 0.1 MHz | 24 | 62 s |
| Fine, ±0.1 MHz @ 0.02 MHz | 6 | 13 s |
| **First point, unknown bank** (survey + coarse + fine) | | **~2.2 min** |
| **First point, known bank** (coarse + fine) | | **~1.3 min** |
| **Warm-started** (±0.3 MHz window) | 12 | **~30 s** |

The survey is paid once per device/bank, not per matrix point. Its frequency
ramp rate may safely be raised above the working 200 kHz/s — low amplitude
means low stored energy — which would cut the 54 s substantially.

Roughly half of each coarse step is the 200 kHz/s frequency ramp rate, which is
a settings value rather than a hardware limit. Raising it would nearly halve
sweep time — but see the rate-dependence test below before doing so.

### Degenerate cases

- **Peak control cannot converge** at some frequency (gain too low to reach
  target within `V_bus_cap`) — record the point as unreachable and continue.
  Unreachability is itself a result. Unreachable across the *entire* window is
  a different matter: see the edge check.
- **Resonance outside the survey range** — a widened survey still showing no
  interior minimum means the bank is wrong. Report; do not widen indefinitely.
- **Anomaly detector fires repeatedly** — the simplifying assumption is wrong.
  Abort, report, revisit the path-aware design.
- **Minima within noise of each other** — record both rather than picking
  arbitrarily.

---

## Phases

### Phase 0 — Mine existing data (no bench time)

Measured `fsw` vs nominal per device and configuration; drift across the
200/300/400 V points; distribution shape. Sets realistic window sizes and
warm-start priors. 563 stored runs already contain this.

### Phase 1 — Diagnostics (bench, 200 V before 400 V)

Three short measurements, all needed before automated search at full voltage.

1. **Gain sweep** — slow open-loop bus ramp at fixed frequency, logging
   `dVds_peak/dV_bus`. Yields `A_max_est` for the safety cap, and confirms the
   peak controller's monotonicity assumption.
2. **Bidirectional check** — one frequency sweep up, then down, at fixed bus.
   Two legs, ~80 s. Quantifies the hysteresis being assumed away. **This
   validates the simplifying assumption rather than presuming it.**
3. **Rate dependence** — repeat (2) at two sweep rates. In a nonlinear
   resonator, sweep speed can affect which branch you land on. This tests
   whether the existing fast manual results are rate-sensitive, and whether the
   frequency ramp rate can safely be raised.

### Phase 2 — Teach the simulator the pathology

`SimulatedRigPlant` is smooth and single-valued, so it cannot exercise any of
this. Add voltage-dependent Coss so it reproduces multiple minima — and
whatever Phase 1 measures of the hysteresis — making the search and its
interlocks testable headlessly before 400 V.

### Phase 3 — Foundations (safe regardless, do early)

- Objective → `P_in = V_bus × I_dc`
- **Trip-context capture** — record frequency, bus setpoint, last Vds peak and
  last `I_dc` into every safety event, with or without an active run. The four
  compliance trips of 29–30 July carry no operating point and cannot be
  diagnosed. **Prerequisite for Phase 1.**
- Replace the fixed-gain peak controller with the bounded secant
- Config checkbox revealing the voltage-only ZVS sweep
- Formalise the soft band (target + 10 V) as distinct from the hard ceiling
- **Record sweep direction on every run.** One column. Nearly free, and the
  only way to detect retrospectively that the simplifying assumption was wrong.

### Phase 4 — Implement the algorithm

Stages 0–4 above, against the upgraded simulator first.

### Phase 5 — Validation

Headless tests: soft band respected, hard ceiling never reached, `V_bus_cap`
enforced, all minima found on a synthetic multi-minimum plant, search
terminates, cancellation honoured mid-sweep. Then bench validation at 200 V
against the manual result for the same point. Only then the full matrix.

## Sequencing

```
Phase 0 (data) ────┐
Phase 1 (diagnostics) ──→ Phase 2 (simulator) ──→ Phase 4 (algorithm) ──→ Phase 5
Phase 3 (foundations) ──┘        ↑
   └── trip-context capture ─────┘  (must precede Phase 1)
```

## What I would not do

- Run an automated frequency search at 400 V before `V_bus_cap` is established
  from measured gain.
- Change the 450 V interlock. An external suggestion proposed 420 V; that
  leaves 10 V above the soft band and would trip routinely.
- Replace the coarse sweep with Brent, golden-section or any local descent.
  The multiple minima are confirmed; coverage is what handles them.
- Raise the frequency ramp rate before the rate-dependence test.
- Discard the anomaly detector or the sweep-direction column. They are the
  cheap insurance against the simplifying assumption being wrong.
- Treat ±20 % as a given. It is a working window derived from a survey or from
  history, not a property of the rig.
- Silently characterise at a resonance far from nominal. Find it, use it,
  and flag that the bank is off design.
