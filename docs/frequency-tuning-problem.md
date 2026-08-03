# Problem brief: joint frequency / bus-voltage optimisation in a resonant GaN test rig

Self-contained statement of an open design problem. No prior context required.

## 1. The rig

We characterise GaN power FETs in a resonant (class-E style) test rig under
automated instrument control.

| Instrument | Role |
|---|---|
| Keithley 2410 SMU | Sources the DC bus through the input choke; its current readback **is** the DC input current |
| Siglent SDG6022X | Gate drive (square wave, 6.5 Vpp, +1.9 V offset) |
| Teledyne LeCroy HDO4054 | Measures Vds peak (P1), RMS current (P2), Isw RMS (P3) |

The test matrix is: device × configuration × frequency × duty × temperature ×
**target Vds peak**.

- Nominal frequencies: 6, 13, 27 MHz
- Target Vds peak: 200, 300, 400 V
- Duty: 25, 50 %
- Temperature: 25, 40, 80 °C
- Configurations: Single Device, Single Conduction, Dual Conduction

Note that the "voltage" axis is the **scope-measured Vds peak**, not the bus
voltage. The bus voltage is whatever is required to produce that peak.

## 2. Why the problem exists

For each nominal frequency, an inductor and capacitor bank are chosen by
ballpark calculation to resonate near that frequency. In practice:

1. The true resonant frequency of the built tank differs from nominal.
2. It **moves during the voltage sweep**, because GaN output capacitance
   Coss is strongly bus-voltage dependent, so the tank's resonance shifts as
   the bus rises.

So the nominal 6 MHz gate drive is generally not the frequency that minimises
losses at any given operating point, and the offset is not constant across the
200/300/400 V points.

## 3. What the software does today

Two closed loops, **both of which move bus voltage only**:

- **Peak controller** — proportional control of the SMU setpoint until
  scope-measured Vds peak is within ±2 V of target. Runs every experiment.
- **ZVS search** — optional fixed-step hill-descent on bus voltage to find the
  DC input-current minimum. Deliberately abandons the peak target when used.

Gate frequency is set once before the run and never revisited. A separate
"autotune" copies the frequency from a *previous* run's measured switching
frequency, but performs no live search.

## 4. The optimisation problem

For a given matrix point, find the gate frequency `f` and bus voltage `V_bus`
that **minimise DC input current `I_dc`**, subject to holding
`Vds_peak(V_bus, f) = target` (200/300/400 V).

Two structural facts make this non-trivial:

- **The constraint set is a curve, not a point.** At fixed `V_bus`,
  `Vds_peak` vs `f` is a resonance curve, so the target peak is reachable on
  *both* sides of resonance. Plotting the required `V_bus` against `f` gives a
  V-shape: minimum at resonance, rising on either side until the SMU can no
  longer supply enough.
- **`I_dc` along that curve is probably not unimodal.** Gain peaks at
  resonance, but minimum switching loss (true ZVS) sits at a phase
  relationship that generally is not the gain peak, and circulating current —
  hence conduction loss — rises with Q. The current minimum is therefore
  offset from the `V_bus` minimum, and there may be one basin per branch.

Concretely: you can always crank the bus until the peak reaches 200 V, but if
the frequency is far off resonance you will draw a lot of current doing it.
Moving the frequency lowers the peak, but the bus can then be raised to restore
it — arriving at the same 200 V peak with lower current.

## 5. Constraints

**Safety.** The rig drives 400 V into live hardware.

- Hard Vds peak ceiling: **450 V** — exceeding it latches a trip and ends the run.
- DC input current ceiling: **1.0 A**; SMU compliance 0.1 A.
- Up to **10 V of Vds peak overshoot is acceptable** during a search. A search
  that routinely trips the interlock is useless, so any search needs its own
  soft band well below the hard ceiling.
- Gate outputs must be armed and read back before the SMU can be enabled;
  every run ends with confirmed shutdown.

**Actuation.**

- Frequency changes are rate-limited (default 200 kHz/s) and can be made while
  the bus is live.
- SMU ramp: 5 V steps at 25 V/s.
- Scope peak reads and SMU current reads take order 100 ms each; safety polling
  runs at ≤250 ms intervals.

**Cost model.** One objective evaluation = change frequency, settle, re-converge
the peak controller, read `I_dc`. That is **seconds**, not milliseconds. The
matrix has hundreds of points, so evaluation count is the binding constraint.
Measurements are noisy.

**Prior data.** 563 historical runs across 7 devices are stored, including each
run's measured switching frequency — usable as a warm start.

## 6. Design currently under consideration

Offered so responses can critique rather than re-derive.

- **Nested, not 2-D.** Outer loop searches frequency; the existing peak
  controller acts as the inner loop, projecting back onto the constraint curve.
  This collapses the search from 2-D to 1-D.
- **Two phases.** A coarse structural map (~10 points across ±20 % of nominal,
  loose peak tolerance) to locate resonance and count basins, then tight
  refinement within the best basin.
- **`V_bus` as a free signal.** The peak controller returns the settled
  setpoint; its minimum over frequency locates resonance at no extra
  measurement cost, identifying the two branches.
- **Adaptive step size** from measured `dPeak/df`, to stay inside the 10 V band
  by construction rather than by luck.
- **Warm starts** from history and from the previous voltage point, since Coss(V)
  drift is monotonic — potentially reducing later points to 3–4 evaluations.

## 7. Known unknowns

1. **Is `Vds_peak` monotonic in `V_bus` at fixed frequency?** The inner-loop
   peak controller is proportional and assumes it is. If Coss(V) nonlinearity
   breaks monotonicity, multiple bus voltages could reach the target peak at one
   frequency, and the controller could settle on either or hunt between them —
   which would make every outer-loop evaluation ambiguous. This has not been
   verified on the bench.
2. **How many basins does `I_dc(f)` actually have** along the constraint curve,
   and how deep is the difference between them?
3. **How far does resonance drift** between the 200, 300 and 400 V points in
   practice? This determines whether warm starting is worth the complexity.

## 8. What would be useful

1. Better algorithms for this optimisation than coarse-map-then-refine, given
   expensive noisy evaluations, probable multimodality, and a strong physical
   prior. Bayesian optimisation, extremum seeking and multi-start local methods
   have been considered; arguments for or against, or something better, welcome.
2. A way to resolve or side-step unknown (1) — either a bench test that settles
   monotonicity, or an inner-loop formulation that does not depend on it
   (e.g. a bracketing solver rather than a P-controller).
3. Whether the objective is right. `I_dc` is the stated target, but input power
   is `V_bus × I_dc`, and the characterisation goal is device loss. Is minimum
   DC input current the correct thing to optimise, or should it be input power,
   or a loss estimate that separates conduction from switching?
4. Whether holding Vds peak exactly on target is the right constraint at all, or
   whether reporting `(f, V_bus, achieved peak)` triples and post-processing
   would yield more useful characterisation data.
