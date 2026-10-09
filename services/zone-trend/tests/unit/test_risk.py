from __future__ import annotations

import pandas as pd
import pytest

from zone_trend.config import load_config
from zone_trend.risk.displacement import DisplacementTable
from zone_trend.risk.liquidation import liquidation_price, stop_within_liq
from zone_trend.risk.portfolio import DAY_MS, HOUR_MS, OpenExposure, PortfolioGuard
from zone_trend.risk.sizing import Filters, size_order
from zone_trend.types import Side, Signal, Trend

CFG = load_config()
FINE = Filters(tick=0.0, step=1e-8, min_qty=0.0, min_notional=5.0)


@pytest.mark.parametrize(
    "stop_pct, notional, risk",
    [(0.01, 125, 1.40), (0.02, 118, 2.50), (0.04, 61, 2.50), (0.06, 41, 2.50)],
)
def test_spec_worked_examples(stop_pct, notional, risk):
    size = size_order(
        100.0,
        100.0 * (1 - stop_pct),
        500,
        500,
        CFG.sizing,
        CFG.exchange,
        FINE,
        CFG.exits.tp1_fraction,
    )
    assert size.notional == pytest.approx(notional, abs=1)
    assert size.risk_usd == pytest.approx(risk, abs=0.01)


def test_fixed_margin_mode():
    sizing = CFG.sizing.model_copy(update={"mode": "fixed_margin"})
    size = size_order(100, 98, 500, 500, sizing, CFG.exchange, FINE, 0.4)
    assert size.notional == pytest.approx(125)


def test_step_rounding_min_notional_and_single_tp():
    btc = Filters(tick=0.1, step=0.001, min_qty=0.001, min_notional=100.0)
    # $125 cap at 1% stop -> 0.00125 BTC at 100k -> 0.001 ($100): TP1 slice too small
    size = size_order(100_000, 99_000, 500, 500, CFG.sizing, CFG.exchange, btc, 0.4)
    assert size.qty == pytest.approx(0.001) and size.single_tp and size.tp1_qty == size.qty
    # 4% stop -> $61 notional -> 0 BTC after rounding: refused
    assert size_order(100_000, 96_000, 500, 500, CFG.sizing, CFG.exchange, btc, 0.4) is None
    eth = Filters(tick=0.01, step=0.001, min_qty=0.001, min_notional=20.0)
    size = size_order(2_000, 1_960, 500, 500, CFG.sizing, CFG.exchange, eth, 0.4)
    assert not size.single_tp and size.tp1_qty == pytest.approx(0.023)


def test_displacement_multiplier_scales_risk_inside_the_cap():
    big = size_order(100, 96, 500, 500, CFG.sizing, CFG.exchange, FINE, 0.4, k_sym=1.5)
    base = size_order(100, 96, 500, 500, CFG.sizing, CFG.exchange, FINE, 0.4)
    capped = size_order(100, 99, 500, 500, CFG.sizing, CFG.exchange, FINE, 0.4, k_sym=1.5)
    assert big.notional == pytest.approx(1.5 * base.notional)
    assert capped.notional == pytest.approx(125)


def test_liquidation_distance_at_5x():
    liq = liquidation_price(100.0, 1.0, +1, 5)
    assert 100 - liq == pytest.approx(100 * (1 - 0.8 / 0.99))  # ~19.2% with a 1% MMR
    short = liquidation_price(100.0, 1.0, -1, 5)
    assert short - 100 == pytest.approx(100 * (1.2 / 1.01 - 1))
    assert stop_within_liq(100, 91, +1, 5) and not stop_within_liq(100, 89, +1, 5)


def test_displacement_table_uses_only_trades_before_the_cutoff():
    rows = [("AAA", "B", t, 3.0, 0.5) for t in range(30)]
    rows += [("BBB", "B", t, 1.0, 0.5) for t in range(30)]
    rows += [("AAA", "B", 1000, 0.0, 0.5)] * 50  # after the cut-off
    trades = pd.DataFrame(rows, columns=["symbol", "entry_type", "exit_time", "mfe_r", "mae_r"])
    table = DisplacementTable.from_trades(trades, cutoff_ms=100, min_trades=30)
    assert table.get("AAA", "B") == 1.5  # 3.0 / median 2.0 = 1.5, at the clamp
    assert table.get("BBB", "B") == 0.5
    assert table.get("CCC", "B") == 1.0


# -- portfolio guard ---------------------------------------------------------------
T0 = 1_700_006_399_999  # a 1H close time (2023-11-15 00:59:59.999 UTC)


def guard() -> PortfolioGuard:
    g = PortfolioGuard(CFG.portfolio, CFG.filters)
    g.mark(T0, 1000.0)
    return g


def sig(symbol="ETHUSDT", side=Side.LONG) -> Signal:
    return Signal("s", symbol, side, "B", T0, 100, 98, 103, 106, 1, "z")


def check(g, s=None, open_pos=(), corr=None, funding=None, btc=None, notional=50.0):
    return g.check(s or sig(), T0, 1000.0, notional, list(open_pos), corr or {}, funding, btc)


def test_daily_loss_pauses_until_next_utc_midnight():
    g = guard()
    g.mark(T0 + HOUR_MS, 971.0)  # -2.9%: still trading
    assert check(g) is None
    g.mark(T0 + 2 * HOUR_MS, 969.0)  # -3.1% against the 00:00 anchor
    assert g.paused_until > T0 and check(g) == "paused"
    assert (g.paused_until + 1) % DAY_MS == 0


def test_drawdown_halves_then_halts():
    g = guard()
    g.mark(T0 + 30 * DAY_MS, 840.0)
    assert g.size_mult() == 0.5 and not g.halted
    g.mark(T0 + 60 * DAY_MS, 749.0)
    assert g.halted and check(g) == "halted"
    g.mark(T0 + 90 * DAY_MS, 1000.0)
    assert g.halted  # manual restart only


def test_consecutive_losses_halve_for_ten_trades():
    g = guard()
    for i in range(CFG.portfolio.consec_losses_halve):
        g.trade_closed(T0 + i, -1.0)
    assert g.size_mult() == 0.5
    for _ in range(10):
        g.trade_opened()
    assert g.size_mult() == 1.0


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"open_pos": [OpenExposure("ETHUSDT", Side.SHORT, 10)]}, "symbol_open"),
        ({"open_pos": [OpenExposure(s, Side.SHORT, 10) for s in ("A", "B", "C")]}, "max_positions"),
        ({"open_pos": [OpenExposure("A", Side.SHORT, 720)]}, "max_notional"),
        (
            {
                "open_pos": [OpenExposure("A", Side.LONG, 10), OpenExposure("B", Side.LONG, 10)],
                "corr": {"A": 0.8, "B": 0.75},
            },
            "correlated",
        ),
        ({"funding": 0.0006}, "funding"),
        ({"s": sig(side=Side.SHORT), "funding": -0.0006}, "funding"),
        ({"btc": Trend.DOWN}, "btc_gate"),
        ({"s": sig(side=Side.SHORT), "btc": Trend.UP}, "btc_gate"),
    ],
)
def test_entry_checks(kwargs, reason):
    assert check(guard(), **kwargs) == reason


def test_correlation_cap_ignores_uncorrelated_and_opposite_positions():
    g = guard()
    pos = [OpenExposure("A", Side.LONG, 10), OpenExposure("B", Side.SHORT, 10)]
    assert check(g, open_pos=pos, corr={"A": 0.9, "B": 0.9}) is None
    assert check(g, s=sig("BTCUSDT"), btc=Trend.DOWN) is None  # BTC itself is not gated
