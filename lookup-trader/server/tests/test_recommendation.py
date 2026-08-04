"""Recommendation policy — verdict branches and caveats."""

from __future__ import annotations

import pytest

from app.services.recommendation import (
    break_even_from_geometry,
    derive_recommendation,
    verdict_rank,
)

_BASE = {
    "side": 1,
    "expectancy_r": 0.12,
    "wilson_low": 0.48,
    "decided": 50,
    "min_samples": 30,
    "level_used": "trend_state+session",
    "break_even_win_rate": 0.4,
}


def test_insufficient_data_when_sample_is_thin():
    result = derive_recommendation(**{**_BASE, "decided": 5})
    assert result["verdict"] == "insufficient_data"
    assert result["headline"] == "Insufficient data"


def test_insufficient_data_on_no_signal():
    result = derive_recommendation(**{**_BASE, "level_used": "no_signal"})
    assert result["verdict"] == "insufficient_data"


def test_wait_on_negative_expectancy():
    result = derive_recommendation(**{**_BASE, "expectancy_r": -0.05, "wilson_low": 0.35})
    assert result["verdict"] == "wait"


def test_wait_when_wilson_low_below_break_even():
    result = derive_recommendation(**{**_BASE, "wilson_low": 0.35})
    assert result["verdict"] == "wait"


def test_buy_when_edge_clears_break_even():
    result = derive_recommendation(**_BASE)
    assert result["verdict"] == "buy"
    assert result["headline"] == "Buy"


def test_sell_for_short_side():
    result = derive_recommendation(**{**_BASE, "side": -1})
    assert result["verdict"] == "sell"
    assert result["headline"] == "Sell"


def test_overlap_caveat():
    result = derive_recommendation(**{**_BASE, "overlap_ratio": 0.6})
    assert any("overlapping" in c for c in result["caveats"])


def test_setup_delta_caveat():
    result = derive_recommendation(**{**_BASE, "setup_delta": 0.005})
    assert any("context prior" in c for c in result["caveats"])


def test_effective_n_caveat():
    result = derive_recommendation(**{**_BASE, "effective_n": 5.0, "decided": 50})
    assert any("independent bars" in c for c in result["caveats"])


def test_break_even_from_geometry():
    assert break_even_from_geometry(1.0, 1.5) == pytest.approx(1 / 2.5)
    assert break_even_from_geometry(0, 1.5) == 0.4


def test_verdict_rank_ordering():
    assert verdict_rank("buy") > verdict_rank("wait")
    assert verdict_rank("sell") > verdict_rank("wait")
    assert verdict_rank("wait") > verdict_rank("insufficient_data")
