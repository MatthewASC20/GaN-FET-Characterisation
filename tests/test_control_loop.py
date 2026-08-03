"""Shared control-loop primitives.

These were duplicated across peak control, the ZVS search and the frequency
search. The risk of divergence was a cancellation honoured in one loop and
swallowed in another, so the behaviour is pinned here once.
"""

from __future__ import annotations

import time

import pytest

from gan_fet.core.control_loop import (
    CANCEL_POLL_INTERVAL_S,
    finite_float,
    is_cancelled,
    settle,
)


def test_is_cancelled_treats_no_predicate_as_running():
    assert is_cancelled(None) is False
    assert is_cancelled(lambda: False) is False
    assert is_cancelled(lambda: True) is True


@pytest.mark.parametrize(
    "value, expected",
    [
        (1.5, 1.5),
        ("2.5", 2.5),
        (0.0, 0.0),
        (-3.0, -3.0),
    ],
)
def test_finite_float_accepts_usable_readings(value, expected):
    assert finite_float(value) == pytest.approx(expected)


@pytest.mark.parametrize(
    "value",
    [None, "", "not a number", float("nan"), float("inf"), float("-inf"), object()],
)
def test_finite_float_rejects_unusable_readings(value):
    """Every unusable case collapses to None: callers treat them identically,
    and distinguishing them would only invite acting on a NaN."""
    assert finite_float(value) is None


def test_settle_waits_the_requested_time():
    started = time.monotonic()
    assert settle(0.15) is True
    assert time.monotonic() - started >= 0.14


def test_settle_reports_cancellation_rather_than_raising():
    """Each loop has its own terminal exception; the helper only decides
    whether the wait was interrupted."""
    assert settle(5.0, lambda: True) is False


def test_settle_notices_cancellation_part_way_through():
    """A long settle must not hide a cancellation for its full duration."""
    started = time.monotonic()
    calls = {"n": 0}

    def cancel_after_a_moment() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    assert settle(10.0, cancel_after_a_moment) is False
    assert time.monotonic() - started < 1.0


def test_settle_sleeps_are_bounded_by_the_poll_interval():
    assert CANCEL_POLL_INTERVAL_S <= 0.05


def test_zero_and_negative_settles_return_immediately():
    assert settle(0.0) is True
    assert settle(-1.0) is True
