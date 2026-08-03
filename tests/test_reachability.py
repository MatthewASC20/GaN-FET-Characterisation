"""Bus bounding, tested directly rather than through a whole search.

Three regimes need three different bounds, and conflating them caused four
separate over-voltage defects during development. Each is asserted here on its
own so a future change cannot quietly collapse them back together.
"""

from __future__ import annotations

import pytest

from gan_fet.core.reachability import ReachabilityGuard

CEILING_V = 429.75
GROWTH = 1.05


def _guard() -> ReachabilityGuard:
    return ReachabilityGuard(ceiling_v=CEILING_V, growth_factor=GROWTH)


def test_no_bound_before_any_gain_is_observed():
    """Bounds must be absent, not zero: a zero cap would forbid every bus."""
    guard = _guard()
    assert guard.local_cap_v is None
    assert guard.prestep_cap_v is None
    assert guard.jump_cap_v is None


def test_cap_keeps_the_peak_under_the_ceiling():
    guard = _guard()
    cap = guard.cap_for(4.0)
    assert cap * 4.0 * GROWTH == pytest.approx(CEILING_V)


def test_local_bound_follows_the_latest_gain_not_the_largest():
    """A bound sized for the highest-gain frequency would forbid safe ones."""
    guard = _guard()
    guard.observe(peak_v=400.0, bus_v=100.0)  # gain 4.0, near resonance
    guard.observe(peak_v=200.0, bus_v=200.0)  # gain 1.0, out at the edge
    assert guard.latest_gain == pytest.approx(1.0)
    assert guard.max_gain == pytest.approx(4.0)
    assert guard.local_cap_v == pytest.approx(guard.cap_for(1.0))
    assert guard.local_cap_v > guard.cap_for(4.0)


def test_jump_bound_follows_the_largest_gain_seen_anywhere():
    """A jump may land near resonance from a low-gain edge, so be pessimistic."""
    guard = _guard()
    guard.observe(peak_v=400.0, bus_v=100.0)
    guard.observe(peak_v=200.0, bus_v=200.0)
    assert guard.jump_cap_v == pytest.approx(guard.cap_for(4.0))
    assert guard.jump_cap_v < guard.local_cap_v


def test_prestep_bound_extrapolates_one_step_ahead():
    """Sizing from present gain is always a step late: the excursion lands on
    arrival, when gain has already risen."""
    guard = _guard()
    guard.record_converged(peak_v=200.0, bus_v=100.0)  # gain 2.0
    guard.record_converged(peak_v=220.0, bus_v=100.0)  # gain 2.2, +10%
    # Next gain predicted as 2.2 * 1.1 = 2.42, tighter than the present bound.
    assert guard.predicted_next_gain == pytest.approx(2.42)
    assert guard.prestep_cap_v < guard.local_cap_v


def test_prestep_extrapolation_ignores_a_falling_gain():
    """A falling gain needs no protection; extrapolating it would loosen the
    bound exactly when the trend reverses."""
    guard = _guard()
    guard.record_converged(peak_v=300.0, bus_v=100.0)  # gain 3.0
    guard.record_converged(peak_v=200.0, bus_v=100.0)  # gain 2.0, falling
    assert guard.predicted_next_gain == pytest.approx(2.0)


def test_implausible_growth_is_clamped():
    """A huge ratio is more likely noise or a transition than a trend."""
    guard = _guard()
    guard.record_converged(peak_v=10.0, bus_v=100.0)   # gain 0.1
    guard.record_converged(peak_v=400.0, bus_v=100.0)  # gain 4.0, x40
    assert guard.predicted_next_gain == pytest.approx(4.0 * 3.0)


def test_sweep_boundary_forgets_step_history_but_not_the_worst_case():
    """Extrapolating across a discontinuity is meaningless, but the pessimistic
    jump bound must survive it."""
    guard = _guard()
    guard.record_converged(peak_v=200.0, bus_v=100.0)
    guard.record_converged(peak_v=400.0, bus_v=100.0)
    guard.start_new_sweep()
    assert guard.predicted_next_gain is None
    assert guard.jump_cap_v == pytest.approx(guard.cap_for(4.0))


@pytest.mark.parametrize(
    "peak_v, bus_v",
    [(None, 100.0), (400.0, 0.0), (400.0, -5.0), (float("nan"), 100.0)],
)
def test_unusable_samples_are_ignored(peak_v, bus_v):
    guard = _guard()
    guard.observe(peak_v=peak_v, bus_v=bus_v)
    assert guard.local_cap_v is None


def test_a_400v_target_still_fits_under_the_bound():
    """The tightest real case: 400 V under a 450 V interlock is only 12%
    headroom, so the growth factor must not make the target unreachable."""
    guard = _guard()
    gain = 4.2
    guard.observe(peak_v=400.0, bus_v=400.0 / gain)
    bus_needed = 400.0 / gain
    assert guard.local_cap_v is not None
    assert guard.local_cap_v >= bus_needed
