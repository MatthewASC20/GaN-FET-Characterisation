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
