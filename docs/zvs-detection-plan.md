# Plan: detecting ZVS directly

Supersedes the ZVS-by-proxy assumption in `frequency-tuning-plan.md`. Written
after the Phase 0 drift analysis and after reviewing 250 scope screenshots from
the previous rig.

## What changed

| | Status |
|---|---|
| Coss(V) raises resonance with voltage | **Confirmed** — 124/124 cases, 200→400 V |
| Tuned frequency departs from nominal | **Measured** — up to 21.7%, median 8.5% |
| Vds has a wide flat dwell at ~0 V before turn-on | **Observed** in every screenshot |
| Ringing on that dwell | ~5–10 V on a 400 V swing (**old rig**) |
| Threshold gating already in use | `(Ch3 < 5)` at 200 V, `(Ch3 < 10)` at 400 V — ~2.5% of peak |
| Scope | Now a LeCroy HDO4054 (screenshots were Tektronix) |

## The central insight

**ZVS onset is a threshold crossing, not a minimum.**

Below the ZVS condition the tank cannot bring Vds to zero before turn-on: the
switch turns on into a residual voltage and ½·Coss·V² is dissipated. At and
beyond it, Vds reaches zero, the body diode conducts, and the drain is *held*
at zero for a dwell that widens as the tank is driven harder.

So the quantity of interest is not where a loss curve bottoms out but where the
dwell **first becomes non-zero**. That difference matters enormously:

| | Loss minimum | ZVS onset |
|---|---|---|
| Shape | non-convex, multiple minima confirmed | monotone threshold crossing |
| Search | sweep for coverage | **bisection** |
| Evaluations | ~30–50 | **~8** |
| Ambiguity | which basin is global? | none |

Bisecting a ±25% window at 6 MHz down to 20 kHz is log₂(3 MHz / 20 kHz) ≈ 8
evaluations. That is roughly a fifth of the current sweep, and it returns an
unambiguous answer rather than a best-so-far.

Note this does **not** replace the loss search — it answers a different
question. Reporting both, and the separation between them, is more useful than
either alone: that gap *is* the circulating-loss penalty of the tank.

## Candidate measurements

**Voltage and current readings only** (`V_bus`, `I_dc`, `P_in`). Available now,
no scope work, already implemented. But these are aggregates: they cannot
separate switching loss from conduction and circulating loss, so they locate a
loss minimum and infer ZVS from it. The current minimum sits systematically off
true ZVS by however much circulating loss the tank contributes — unknown, and
build-dependent.

**Waveform capture and host-side analysis.** Most flexible and most robust: the
algorithm is entirely under our control, and the bounded binary transfer at the
SCPI session boundary already exists for screenshots. Cost is transfer time per
point, which is too slow for every step of a search but ideal for
characterising the measurement itself.

**Scope math plus a parameter (recommended for the search loop).** One scalar
read per point, same cost as the existing P1–P3 reads, and it fits the design
contract in which the operator configures the measurement table and the
software reads it.

## The metric: dwell *fraction*, not dwell duration

Measure the proportion of the cycle Vds spends below a threshold — a duty at
level, not a width.

**This matters because the search sweeps frequency.** A dwell of 20 ns means
something different at 6 MHz (167 ns period) than at 27 MHz (37 ns). A duration
cannot be compared across the sweep; a fraction is self-normalising.

Threshold at **~2.5% of Vds peak**, which is the scaling already arrived at
empirically on the previous rig — 5 V at 200 V, 10 V at 400 V.

Two consequences to handle:

- **The threshold must scale with the target voltage.** That sits awkwardly
  with the rule that the software reads the measurement table rather than
  defining it. Either the operator reconfigures per voltage point, or this
  becomes a deliberate, documented exception. It is the one place the software
  would need to write a measurement setting.
- **Ringing and threshold are comparable in size.** At 5–10 V of ripple against
  a 5–10 V threshold, a bare comparator will chatter during the dwell.
  Hysteresis, or a light low-pass on the math trace before thresholding, is
  required — sized from measured ringing, not assumed.

## Plan

### Phase A — characterise the measurement (new rig, low voltage first)

Capture Vds waveforms at several frequencies spanning the window, at 200 V
before 400 V. From them, determine:

1. Actual ringing amplitude and frequency on the new build, and whether any of
   it exceeds the scope's bandwidth.
2. Dwell width and where it sits relative to turn-on.
3. A threshold and hysteresis that are stable across the sweep.
4. **Whether dwell fraction is monotone in frequency** over the window — the
   assumption bisection rests on.

This is the gate. Everything downstream depends on (4).

### Phase B — configure the scope

Add the dwell-fraction parameter as P4, referenced to a gate-drive trigger so
turn-on stays at t=0 as frequency changes. Confirm exact MAUI parameter and
gating property names against the manual; the driver's existing
`VBS? 'return=app.Measure.Px.Out.Result.Value'` pattern reads it unchanged.

Extend the simulated plant with a dwell model so the search logic is testable
headlessly, as was done for the resonance.

### Phase C — implement ZVS-onset search

Bracket with the existing survey — one frequency with zero dwell, one with
positive — then bisect on the crossing. Reuse the safety machinery already
built: adaptive stepping, predictive peak guard, pre-step back-off, jump
protection. None of that changes; only the objective and the search shape do.

### Phase D — record both operating points

Store the ZVS-onset frequency alongside the `P_in` minimum, and their
separation. Schema 5 already carries `tuned_frequency_hz` and
`tuned_input_power_w`; this adds the onset frequency and dwell fraction.

## What stays as it is

- The `P_in` objective and the coverage sweep. They answer the loss question,
  which is still worth answering, and they work today.
- Every safety mechanism. The ZVS search moves frequency exactly as the loss
  search does, and inherits the same guards unchanged.
- Ascending voltage order through the matrix, per the Phase 0 finding.

## Open questions

1. Is dwell fraction monotone in frequency across the window on the real rig?
   Bisection is invalid if not, and the search falls back to a sweep.
2. How much ringing does the new build have? The 5–10 V figure describes a rig
   that no longer exists.
3. Does the HDO4054 offer a duty-at-level parameter directly, or does this need
   a math trace first? Affects Phase B only, not the approach.
4. Should the software be allowed to write the threshold when the target
   voltage changes, or does the operator own it? This is a design-contract
   decision, not a technical one.

## The simulated plant cannot validate the objective (2026-08-01)

Investigating a `--simulate` log that settled at 5.2408 MHz, I swept the
simulated plant from 4.6–7.4 MHz holding Vds peak at 200 V:

| f (MHz) | gain | I (mA) | P_in (W) | dwell |
|---|---|---|---|---|
| 5.30 | 2.11 | 23.5 | **2.243** ← the search picks this | **0.000** |
| 6.00 | 3.00 | 35.2 | 2.333 | 0.000 |
| 6.50 | 4.19 | 65.2 | 3.113 | **0.170** ← best ZVS |

**In the simulated plant, minimum `P_in` and ZVS are anti-correlated.** The ZVS
point costs 39% more input power, so the search correctly minimises its
objective and lands somewhere with no ZVS at all. Confirmed not to be a
warm-start artefact: eight generations feeding each result forward are stable
at 5.30–5.34 MHz, and a full ±25% window — which contains the model's
resonance at 6.39 MHz — also chooses 5.31 MHz.

The cause is in `SimulatedRigPlant._resonant_loss_a`. Its switching-loss term
is shaped by a Lorentzian and a `(1 + 3.5·detuning)` tilt, which makes it
*largest* on the high side — exactly where the same model's
`zvs_dwell_fraction` is largest. Switching loss is the loss ZVS eliminates, so
the two should move oppositely. Circulating loss (35 mA at resonance) also
outweighs switching loss (10 mA maximum) by 3.5×, so total current simply
peaks at resonance with nothing to pull the minimum back toward it.

**The operator confirms that on the bench the two coincide** — the manual
method is to find ZVS near the target peak and minimise current around it. The
model is therefore wrong about this rig, and until it is fixed, `--simulate`
validates the search's mechanics only: window coverage, peak holding, safety
ordering, cancellation. It cannot catch a regression in the objective, and a
simulated run that reports success has not demonstrated that the search finds
ZVS. `tests/test_frequency_tune.py` already says as much in its docstring,
which is why nothing flagged this.

Two consequences, one addressed:

- **Addressed.** `TuneResult` now carries the dwell measured at the settled
  winner, and a *measured* zero raises a warning to the operator and a
  `frequency_tune_no_zvs` safety event against the run. Because the two
  coincide on this rig, a zero-dwell winner means the search converged on a
  shoulder outside the resonant basin. `None` (unmeasurable) is deliberately
  not treated as zero.
- **Open.** Near the chosen point the objective is flat to within the
  measurement noise: `P_in` spans 2.24–2.31 W across 5.2–5.5 MHz while
  *adjacent* points differ by ~0.3%. Since `P_in ∝ V_peak²`, the ±2 V
  peak-control tolerance is worth ~2% — larger than the signal being
  discriminated. Normalising each `P_in` by `(target / achieved_peak)²` would
  remove most of it. In simulation the peak lands at 201.5 V consistently so
  this changes little; on the bench, where peak control will be less
  repeatable, it is predicted to dominate.

### Next, in order

1. Tie the plant's switching-loss term to the dwell the model already
   computes, so `P_in` dips where ZVS turns on. Without this the simulator
   cannot validate the thing the search exists to do.
2. Normalise `P_in` for peak error.
3. Reduce wasted frequency travel: entering the window from its far floor and
   two full-length returns to best cost ~10 s of the 28 s search in the log.
