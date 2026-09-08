"""Unit pins for the pure contingent-hedge triggers in ``entry.hedge_pair``.

These moved verbatim out of the engine; the engine-level suite
(``test_entry_modes``) and the determinism gate pin the integrated behaviour.
"""

from __future__ import annotations

from backtesting_service.entry import (
    contingent_hedge_fill,
    contingent_hedge_touched,
    failure_threshold,
)


def test_failure_threshold_sits_inside_the_stop() -> None:
    assert failure_threshold(entry=100, sl_dist=10, is_long=True, hedge_failure_k=0.5) == 105
    assert failure_threshold(entry=100, sl_dist=10, is_long=False, hedge_failure_k=0.5) == 95


def test_failure_threshold_k_zero_is_the_stop() -> None:
    assert failure_threshold(entry=100, sl_dist=10, is_long=True, hedge_failure_k=0) == 110
    assert failure_threshold(entry=100, sl_dist=10, is_long=False, hedge_failure_k=0) == 90


def test_touch_is_inclusive_on_the_primary_side() -> None:
    assert contingent_hedge_touched(threshold=95, bar_low=95, bar_high=110, long_primary=True)
    assert not contingent_hedge_touched(threshold=95, bar_low=95.5, bar_high=110, long_primary=True)
    assert contingent_hedge_touched(threshold=105, bar_low=90, bar_high=105, long_primary=False)
    assert not contingent_hedge_touched(
        threshold=105, bar_low=90, bar_high=104.5, long_primary=False
    )


def test_fill_uses_the_open_on_a_gap_through() -> None:
    assert contingent_hedge_fill(bar_open=90, threshold=95, long_primary=True) == 90
    assert contingent_hedge_fill(bar_open=97, threshold=95, long_primary=True) == 95
    assert contingent_hedge_fill(bar_open=110, threshold=105, long_primary=False) == 110
    assert contingent_hedge_fill(bar_open=103, threshold=105, long_primary=False) == 105
