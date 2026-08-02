# Phase 0: measured frequency drift

Analysis of 460 completed runs carrying a measured switching frequency,
recorded June–December 2025. Run to size the frequency search from data rather
than from guesses.

**Provenance caveat.** Every one of these runs predates the lab move and was
taken on the previous rig with a Tektronix scope. Absolute resonance is a
property of the tank as built and does **not** carry over. Relative drift with
voltage is driven by Coss(V), a device property, and should. Everything below
is used only for *relative* quantities.

## 1. How far the tuned frequency departs from nominal

| Nominal | n | min | median | max |
|---|---|---|---|---|
| 6 MHz | 261 | −18.33% | +5.00% | +21.67% |
| 13 MHz | 159 | −20.77% | +1.54% | +17.69% |
| 27 MHz | 40 | −15.56% | −0.74% | +13.33% |

Distribution of `|deviation|` across all bands:

| p50 | p75 | p90 | p95 | p99 | max |
|---|---|---|---|---|---|
| 8.46% | 13.33% | 15.56% | 16.67% | 21.67% | 21.67% |

**Finding: the ±20% working window was too narrow.** The observed maximum is
21.67%, so the worst historical cases would have been clipped at the window
edge — the search would have returned a boundary value and flagged
`clipped_at_edge`, but only after wasting a full sweep.

`window_frac` raised to **0.25**.

Consistency across devices, i.e. whether one window fits all:

| Device | n | median | p90 | max |
|---|---|---|---|---|
| 145D2 | 85 | 8.46% | 15.00% | 16.67% |
| EPC2307 | 54 | 9.23% | 16.92% | 17.69% |
| GPI65010 | 41 | 11.67% | 13.33% | 13.33% |
| GPI65015DFN | 112 | 8.46% | 13.33% | 16.67% |
| GPI65030DFN | 116 | 10.00% | 20.00% | 21.67% |
| GS66504B | 52 | 6.67% | 10.00% | 10.00% |

Medians cluster between 6.7% and 11.7%, so a single window size is defensible;
GPI65030DFN sets the requirement.

## 2. Drift with target Vds peak

| Step | n | median | p90 \|d\| | max \|d\| | rising |
|---|---|---|---|---|---|
| 200 → 300 V | 133 | +4.62% | 9.52% | 16.67% | 131/133 |
| 300 → 400 V | 128 | +2.17% | 5.56% | 7.14% | 120/128 |
| 200 → 400 V | 124 | +6.25% | 15.87% | 16.50% | **124/124** |

**Finding: the Coss(V) prediction is confirmed.** The tuned frequency rises
with voltage in 94–100% of adjacent steps, and in *every* one of the 124 cases
spanning 200 V to 400 V. Falling Coss raising the resonance is not a
hypothesis about this rig; it is what the data shows.

| Nominal | adjacent steps rising |
|---|---|
| 6 MHz | 150/160 (94%) |
| 13 MHz | 93/93 (100%) |
| 27 MHz | 8/8 (100%) |

**Finding: drift decelerates with voltage.** 200→300 V moves roughly twice as
far as 300→400 V, consistent with Coss flattening at higher Vds. Warm starting
from an adjacent voltage point is therefore more reliable at the top of the
range than the bottom.

**Finding: the warm-start window was the wrong shape.** It was an absolute
300 kHz, but drift scales with the band — 300 kHz is 5% at 6 MHz and only 1.1%
at 27 MHz, so the same setting was loose at the bottom of the matrix and far
too tight at the top.

`warm_window_hz` replaced by `warm_window_frac = 0.05`.

## 3. What could not be measured

- **The survey amplitude offset.** The survey runs at ~18% of target peak, well
  below the 200 V floor of this dataset. EPC2307 has 100 V runs but no 200 V
  counterparts, so no 100→200 V pairs exist and the curve cannot be extended
  downward. `survey_upward_bias_frac` was raised from 0.03 to **0.08** by
  extrapolating the +6.25% seen across 200→400 V, and is explicitly a guess.
  Precision is not critical: the ±25% window dominates, and the bias only has
  to place the true optimum comfortably inside it. Phase 1 should measure it.
- **Run-to-run repeatability.** Zero matrix points were run twice successfully,
  so the scatter of a repeated measurement at a fixed point is unknown. This is
  also why any path dependence in the existing data is invisible.
- **Multiple minima.** The stored data records one tuned frequency per run, not
  the landscape that was searched to find it, so it cannot corroborate the
  multiple minima observed manually.

## 4. Settings changed

| Setting | Was | Now | Basis |
|---|---|---|---|
| `window_frac` | 0.20 | **0.25** | max observed deviation 21.67% |
| `warm_window_hz` → `warm_window_frac` | 300 kHz | **0.05** | drift scales with band, not absolute Hz |
| `survey_upward_bias_frac` | 0.03 | **0.08** | extrapolated from +6.25% across 200→400 V |

## 5. Operational consequence

Because drift is essentially always upward with voltage, **the matrix should be
run in ascending voltage order** for warm starts to be useful. Descending
order would seed each search on the wrong side of the true optimum, and with a
±5% warm window that is enough to miss it.
