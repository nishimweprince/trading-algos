import pytest
import pandas as pd

from app.services.labeler import label_triple_barrier


def _make_candles(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def test_long_win():
    candles = _make_candles([
        (100, 101, 99, 100),
        (100, 102, 99, 101),   # signal at idx 0
        (101, 105, 100, 104),  # hits tp=104
    ])
    result = label_triple_barrier(candles, 0, side=1, entry=100, sl=98, tp=104, max_bars=5)
    assert result["result"] == "win"
    assert result["realized_r"] == pytest.approx(2.0)


def test_long_loss():
    candles = _make_candles([
        (100, 101, 99, 100),
        (100, 101, 97, 98),   # hits sl=98
    ])
    result = label_triple_barrier(candles, 0, side=1, entry=100, sl=98, tp=104, max_bars=5)
    assert result["result"] == "loss"
    assert result["realized_r"] == pytest.approx(-1.0)


def test_short_win():
    candles = _make_candles([
        (100, 101, 99, 100),
        (100, 101, 94, 95),   # hits tp=95 for short
    ])
    result = label_triple_barrier(candles, 0, side=-1, entry=100, sl=102, tp=95, max_bars=5)
    assert result["result"] == "win"


def test_timeout():
    candles = _make_candles([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
    ])
    result = label_triple_barrier(candles, 0, side=1, entry=100, sl=90, tp=110, max_bars=2)
    assert result["result"] == "timeout"
    assert result["realized_r"] is None


def test_ambiguous_conservative():
    candles = _make_candles([
        (100, 101, 99, 100),
        (100, 105, 95, 100),  # both tp and sl hit
    ])
    result = label_triple_barrier(
        candles, 0, side=1, entry=100, sl=98, tp=104, max_bars=5, ambiguous="conservative"
    )
    assert result["result"] == "loss"


def test_ambiguous_drop():
    candles = _make_candles([
        (100, 101, 99, 100),
        (100, 105, 95, 100),
    ])
    result = label_triple_barrier(
        candles, 0, side=1, entry=100, sl=98, tp=104, max_bars=5, ambiguous="drop"
    )
    assert result["result"] == "ambiguous"


def test_ambiguous_optimistic():
    candles = _make_candles([
        (100, 101, 99, 100),
        (100, 105, 95, 100),
    ])
    result = label_triple_barrier(
        candles, 0, side=1, entry=100, sl=98, tp=104, max_bars=5, ambiguous="optimistic"
    )
    assert result["result"] == "win"


def test_signal_at_last_bar():
    candles = _make_candles([(100, 101, 99, 100)])
    result = label_triple_barrier(candles, 0, side=1, entry=100, sl=98, tp=104, max_bars=5)
    assert result["result"] == "timeout"


def test_ambiguity_is_flagged_even_when_policy_hides_it():
    """The conservative policy turns an ambiguous bar into a loss. Without the
    flag that choice is unrecoverable — the row looks like a clean stop-out."""
    candles = _make_candles([
        (100, 101, 99, 100),
        (100, 105, 95, 100),  # both barriers in one bar
    ])
    result = label_triple_barrier(
        candles, 0, side=1, entry=100, sl=98, tp=104, max_bars=5, ambiguous="conservative"
    )
    assert result["result"] == "loss"
    assert result["ambiguous_bar"] is True


def test_clean_resolution_is_not_flagged_ambiguous():
    candles = _make_candles([
        (100, 101, 99, 100),
        (101, 105, 100, 104),
    ])
    result = label_triple_barrier(candles, 0, side=1, entry=100, sl=98, tp=104, max_bars=5)
    assert result["ambiguous_bar"] is False


def test_timeout_carries_mark_to_close():
    """realized_r stays None on a timeout, but r_at_horizon makes the row usable."""
    candles = _make_candles([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 102, 100, 101),
    ])
    result = label_triple_barrier(candles, 0, side=1, entry=100, sl=90, tp=110, max_bars=2)
    assert result["result"] == "timeout"
    assert result["realized_r"] is None
    assert result["r_at_horizon"] == pytest.approx(0.1)  # (101 - 100) / 10


def test_long_excursions_and_r_grid():
    """Ran to +2.4R, then stopped out. The stored result is a loss; the path says
    a 2R target would have paid."""
    candles = _make_candles([
        (100, 101, 99, 100),    # signal
        (100, 102, 100, 101),   # +2R touched (risk = 1)
        (101, 102.4, 100, 101), # peak +2.4R
        (101, 101, 99, 99),     # sl = 99
    ])
    result = label_triple_barrier(
        candles, 0, side=1, entry=100, sl=99, tp=105, max_bars=10,
        r_grid_targets=[1.0, 2.0, 3.0], pip_size=0.1,
    )
    assert result["result"] == "loss"
    assert result["mfe_r"] == pytest.approx(2.4)
    assert result["mae_r"] == pytest.approx(-1.0)
    assert result["bars_to_mfe"] == 2
    assert result["mfe_pips"] == pytest.approx(24.0)  # 2.4R * risk 1.0 / pip 0.1
    assert result["r_grid"]["1.0"]["result"] == "win"
    assert result["r_grid"]["2.0"]["result"] == "win"
    assert result["r_grid"]["3.0"]["result"] == "loss"
    assert result["touched_1r_before_sl"] is True


def test_short_excursions_use_lows_for_favourable():
    candles = _make_candles([
        (100, 101, 99, 100),    # signal
        (100, 100, 97.6, 98),   # short favourable move to 97.6 = +2.4R (risk 1)
        (98, 101, 98, 101),     # sl = 101
    ])
    result = label_triple_barrier(
        candles, 0, side=-1, entry=100, sl=101, tp=95, max_bars=10,
        r_grid_targets=[1.0, 2.0, 3.0],
    )
    assert result["result"] == "loss"
    assert result["mfe_r"] == pytest.approx(2.4)
    assert result["mae_r"] == pytest.approx(-1.0)
    assert result["r_grid"]["2.0"]["result"] == "win"
    assert result["r_grid"]["3.0"]["result"] == "loss"


def test_exit_price_matches_the_barrier_that_resolved():
    candles = _make_candles([
        (100, 101, 99, 100),
        (101, 105, 100, 104),
    ])
    result = label_triple_barrier(candles, 0, side=1, entry=100, sl=98, tp=104, max_bars=5)
    assert result["exit_price"] == 104


def test_entry_outside_every_bar_is_infeasible():
    """Entry is placed by clicking the chart, so it can name a price the market
    never traded — a trade that could not have been taken."""
    candles = _make_candles([
        (100, 101, 99, 100),
        (100, 101, 99.5, 100),
        (100, 101, 99.5, 100),
    ])
    result = label_triple_barrier(candles, 0, side=1, entry=90, sl=88, tp=95, max_bars=5)
    assert result["entry_feasible"] is False

    feasible = label_triple_barrier(candles, 0, side=1, entry=100, sl=98, tp=105, max_bars=5)
    assert feasible["entry_feasible"] is True
