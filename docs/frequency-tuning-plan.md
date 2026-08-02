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

#### Proposed changes (2026-08-01)

Voltage-dependent Coss and the multi-minimum structure are in. What is wrong
is the loss model that sits on top of them. Measured from the plant, holding
Vds peak at 200 V:

| f (MHz) | gain | I (mA) | P_in (W) | dwell |
|---|---|---|---|---|
| 5.30 | 2.11 | 23.5 | **2.243** ← the search picks this | **0.000** |
| 6.50 | 4.19 | 65.2 | 3.113 | **0.170** ← best ZVS |

Minimum `P_in` and ZVS are anti-correlated, so the search lands somewhere with
no ZVS at all and simulation cannot validate the objective. **The operator
confirms the two coincide on the bench**, so the model, not the search, is
wrong.

The cause is `_resonant_loss_a`. Its switching term is shaped by a Lorentzian
and a `(1 + 3.5·detuning)` tilt, which makes it largest on the high side —
exactly where the same object's `zvs_dwell_fraction` is largest. Switching
loss is the loss ZVS removes; the two must not be free to disagree.

**Proposal: derive both loss terms from the tank gain that already decides the
dwell, so they cannot drift apart.**

1. **Switching loss.** The FET turns on into a charged `Coss`, dissipating
   `½·Coss·V_residual²` each cycle. Model the residual voltage, not the
   frequency:

   ```
   approach     = min(1, gain / zvs_onset_gain)      # partial pull-down pre-onset
   completeness = min(1, dwell / zvs_full_dwell)     # ZVS proper, post-onset
   residual     = (1 − partial_zvs_frac·approach)·(1 − completeness)
   switching    = switching_loss_a · residual²
   ```

   Below onset the tank pulls the drain partway down and the loss falls
   smoothly; past onset the dwell finishes the job and it collapses. Squared,
   because the energy goes as voltage squared.

2. **Circulating loss.** Conduction loss from tank current, which scales with
   how hard the tank is driven: `circulating_loss_a · (gain / resonant_peak_gain)²`.
   Rises monotonically toward resonance.

3. **Their sum has its minimum at or just past ZVS onset** — just enough
   circulating current to achieve ZVS and no more, which is the class-E design
   point and what the operator finds by hand. Below onset switching loss
   dominates and falls; above it circulating loss dominates and rises.

Four new named parameters (`switching_loss_a`, `circulating_loss_a`,
`zvs_full_dwell`, `partial_zvs_frac`) rather than the current inline
constants, so the shape can be refitted when Phase 1 measures the real rig.

**Acceptance, checked numerically rather than by eye:**

- the `P_in` minimum has **non-zero dwell** — the property the whole change
  exists for, and the one the old model failed;
- the minimum is near onset, not at maximum dwell (excess circulating current
  is a real cost, not a free lunch);
- more than one local minimum survives across the window, so the search still
  has to compare basins rather than descend into the nearest;
- gain behaviour is untouched: `_tank_state` is not modified, so the peak
  controller and every reachability test see exactly what they saw before.

**Not claimed:** this is still not a circuit model. It encodes one
relationship — that ZVS is what removes switching loss — because that is the
relationship the search exists to exploit and the one whose absence made every
simulated run look successful while choosing a non-ZVS point.

#### Implemented — measured result

One correction to the proposal above: the loss terms are **watts, not amps**.
The bus falls by half across this window as gain rises, so a loss expressed as
a current silently changes meaning as the search moves. Expressed as power and
divided by the bus it does not — and both terms scale with voltage squared, so
the implied current falls to zero with the bus rather than dividing by it,
which matters because every run ramps through zero.

That also supplied the term the first attempt was missing. Switching loss goes
as `V_residual²`, and off resonance the bus is *highest* exactly where the tank
helps least, so hard switching there is expensive twice over. Without it the
falling bus dominates and `P_in` simply decreases toward resonance no matter
what the residual does — the first attempt put the minimum at maximum dwell and
left a single flat basin.

Constants: `switching_loss_w = 3.5`, `circulating_loss_w = 1.5`,
`zvs_full_dwell = 0.06`, `partial_zvs_frac = 0.40`, chosen by scanning the
parameter space against the acceptance criteria rather than by eye. Measured
over 4.6–7.4 MHz at 200 V peak:

| | before | after |
|---|---|---|
| `P_in` minimum | 5.30 MHz | **6.20 MHz** |
| dwell there | **0.000** | **0.054** |
| ZVS onset | 6.10 MHz | 6.10 MHz |
| maximum dwell | 6.50 MHz (costs +39%) | 6.55 MHz (costs +13%) |
| basins | 1 | 2 (6.20 and 6.85, both with ZVS) |
| well depth | — | 53% |
| current range | 23–65 mA | 42–61 mA |

The minimum now sits 100 kHz past onset and is *not* at maximum dwell: driving
harder buys no more ZVS and costs conduction loss, which is the class-E design
point and what the operator finds by hand.

Five tests pin this, all of which fail against the previous model. `_tank_state`
is untouched, so every gain, peak-control and reachability test sees exactly
what it saw before.

**Known limitation.** The two basins differ by only 1.4% in `P_in`, while the
±2 V peak-control tolerance is worth ~2%. Which basin wins is therefore partly
noise — the simulator now reproduces that difficulty rather than hiding it,
which is the argument for doing the `P_in` normalisation next. No test asserts
*which* basin wins, only that both exist and both have ZVS.

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

#### First simulated run against the new model (2026-08-02)

Confirms the change end to end, and turned up three things worth fixing.

**It works.** The run settled at **6.8331 MHz, bus 55.41 V, Vds peak 200.03 V,
P4 dwell 7.05**. The previous run, against the old model, settled at 5.2408 MHz
with dwell 0.000. The no-ZVS warning correctly stays silent.

**The escalation earned its place.** The warm start was 5,240,831 Hz — the
previous run's no-ZVS answer. Its ±5% window (4.979–5.503 MHz) put the best
point on an edge, so the warm start was discarded and a survey run at 18%
amplitude (bus 10.95 V, peak 35.8 V) across 3–9 MHz located resonance near
6.4 MHz. The wide re-sweep then found ZVS across 6.10–6.99 MHz. Without that
escalation the run would have stayed in the dead region.

##### Open: a 402 V transient at a 200 V target

Returning to the winner from 8.59 MHz down to 6.77 MHz, P1 read **402.3 V**
with the bus at 97.5652 V.

That is the reachability guard behaving exactly as specified. `cap_for(gain)`
is `ceiling_v / (gain × growth_factor)` = 429.75 / (4.19 × 1.05) = 97.68 V,
matching the bus observed, and the resulting peak sits under both the 429.75 V
cap and the 450 V interlock. No trip, by construction.

The problem is that the cap is **absolute**. It bounds the peak against the
interlock — which was set for the 400 V test — and not against *this run's*
target. At a 400 V target that permits at most 7.5% over. At a 200 V target it
permits 100% over, which is what happened. A device only ever intended for
200 V would see 402 V during a routine return-to-winner.

The 450 V interlock should not move. The fix is a second, tighter per-run bound
— a jump cap of roughly `target + soft_band` — so an excursion is bounded by
the operating point as well as by what the rig can survive.

##### Open: 3 min 40 s for a six-second validation

| phase | time |
|---|---|
| survey (ramp to 3 MHz, sweep to 9 MHz, return) | **89 s** |
| wide coarse sweep | 70 s |
| stale warm-start sweep, wasted | 21 s |
| return to winner + fine sweep | 26 s |
| the actual run | 6 s |

Almost all of the survey is *travel*, not measurement. Every frequency move
ramps at 10 kHz per ~55 ms, so one 200 kHz survey step costs 1.1 s in twenty
wavegen writes, and the 6→3 MHz approach alone costs 16 s. The survey runs at
18% amplitude precisely because the tank is near-linear there, so the slow ramp
buys nothing — and it is crossing ground it is about to sweep anyway. Ramping
at the survey step size would cut roughly 70 s.

##### Open: a warm start with no ZVS should not be trusted

21 s went into sweeping a window seeded by an answer already known to have zero
dwell. Now that dwell is recorded at the winner, a stored frequency whose dwell
was measured as zero is evidence the previous run converged in the wrong place,
and is worth less than no warm start at all.
