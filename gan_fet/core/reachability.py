"""Bus bounding that makes an over-voltage operating point unreachable.

Tank gain rises steeply as a frequency sweep approaches resonance, and a
branch transition completes in microseconds against a 250 ms safety poll.
Detecting an excursion is therefore a race that cannot be won; the bus is
bounded instead, so the peak the rig *can* reach stays below the interlock by
construction.

Three regimes need three different bounds, and conflating them was the source
of four separate defects during development:

* **within a convergence** — gain is known locally, so the bound follows the
  gain measured here and now;
* **across one sweep step** — the excursion happens on arrival, when gain is
  already higher, so the bound follows gain extrapolated one step ahead;
* **across a discontinuous jump** — nothing is known about the destination, so
  the bound follows the highest gain seen anywhere.

Extracted from the frequency tuner so this logic can be exercised directly
rather than only through a full search.
"""

from __future__ import annotations

import math
from typing import Optional

#: Largest step-to-step gain growth the extrapolation will believe. Beyond
#: this the reading is more likely noise or a transition than a trend, and
#: trusting it would loosen the bound exactly when it should tighten.
MAX_CREDIBLE_GROWTH_RATIO = 3.0


class ReachabilityGuard:
    """Tracks tank gain and converts it into safe bus-voltage bounds."""

    def __init__(self, ceiling_v: float, growth_factor: float) -> None:
        #: Search ceiling, deliberately below the hard interlock so a sweep
        #: stops rather than trips.
        self.ceiling_v = float(ceiling_v)
        #: Headroom above observed gain. Must stay near unity: a 400 V target
        #: under a 450 V interlock leaves only 12%, so a large blanket margin
        #: makes the target itself unreachable.
        self.growth_factor = float(growth_factor)
        self._latest_gain = 0.0
        self._max_gain = 0.0
        self._converged_gains: list[float] = []

    # -- observation -------------------------------------------------------

    def observe(self, peak_v: Optional[float], bus_v: float) -> None:
        """Record a gain sample seen at any point during the search."""
        gain = self._gain(peak_v, bus_v)
        if gain is None:
            return
        self._latest_gain = gain
        self._max_gain = max(self._max_gain, gain)

    def record_converged(self, peak_v: Optional[float], bus_v: float) -> None:
        """Record gain at a settled, on-target point.

        Kept separate from ``observe`` because only converged points are
        comparable to each other, and the step-ahead extrapolation is a
        comparison between consecutive ones.
        """
        gain = self._gain(peak_v, bus_v)
        if gain is None:
            return
        self.observe(peak_v, bus_v)
        self._converged_gains.append(gain)

    def start_new_sweep(self) -> None:
        """Forget the step history at a sweep boundary.

        Extrapolation across a discontinuity is meaningless; the jump bound
        covers that case instead.
        """
        self._converged_gains.clear()

    @staticmethod
    def _gain(peak_v: Optional[float], bus_v: float) -> Optional[float]:
        if peak_v is None or bus_v <= 1e-6:
            return None
        gain = peak_v / bus_v
        return gain if math.isfinite(gain) and gain > 0.0 else None

    # -- bounds ------------------------------------------------------------

    def cap_for(self, gain: float) -> float:
        """Bus voltage that keeps the peak under the ceiling at this gain."""
        effective = max(1e-6, gain * self.growth_factor)
        return max(0.0, self.ceiling_v / effective)

    @property
    def local_cap_v(self) -> Optional[float]:
        """Bound for converging here, from the gain measured here.

        Deliberately the latest gain rather than the largest: gain varies
        several-fold across a window, and a bound sized for the highest-gain
        frequency would forbid perfectly safe lower-gain ones.
        """
        if self._latest_gain <= 0.0:
            return None
        return self.cap_for(self._latest_gain)

    @property
    def prestep_cap_v(self) -> Optional[float]:
        """Bound for stepping frequency, from gain one step ahead.

        Sizing this from present gain is always a step late: the excursion
        lands on arrival, when gain has already risen.
        """
        predicted = self.predicted_next_gain
        if predicted is None or predicted <= 0.0:
            return None
        return self.cap_for(predicted)

    @property
    def jump_cap_v(self) -> Optional[float]:
        """Bound for a discontinuous move, from the worst gain seen anywhere.

        A jump may land near resonance from a low-gain window edge, so there
        is no basis for prediction and conservatism is the only safe choice.
        """
        if self._max_gain <= 0.0:
            return None
        return self.cap_for(self._max_gain)

    @property
    def predicted_next_gain(self) -> Optional[float]:
        """Gain extrapolated one step ahead, upward growth only.

        A falling gain needs no protection, so the ratio is floored at 1.0 —
        extrapolating a decrease would loosen the bound.
        """
        history = self._converged_gains
        if len(history) >= 2:
            previous, latest = history[-2], history[-1]
            if previous > 0.0:
                ratio = min(max(latest / previous, 1.0), MAX_CREDIBLE_GROWTH_RATIO)
                return latest * ratio
            return latest
        return history[-1] if history else None

    # -- introspection -----------------------------------------------------

    @property
    def latest_gain(self) -> float:
        return self._latest_gain

    @property
    def max_gain(self) -> float:
        return self._max_gain
