from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from zone_trend.backtest.broker_sim import Broker, LiquidationError, PendingEntry
from zone_trend.backtest.market import MinuteBars
from zone_trend.backtest.metrics import max_drawdown, profit_factor
from zone_trend.risk.sizing import Size
from zone_trend.types import Side, Signal

HOUR = 3_600_000
MIN = 60_000
H0 = 1_700_000_000_000 // HOUR * HOUR
FEE = 0.0005
SLIP = 0.001


class FakeMinutes:
    """Span start -> list of (open, high, low, close[, mark_low, mark_high]) minutes."""

    def __init__(self, hours: dict[int, list[tuple]]):
        self.hours = hours
        self.mark_missing = 0

    def window(self, symbol: str, start: int, span_ms: int) -> MinuteBars:
        rows = self.hours.get(start, [])
        n = len(rows)
        cols = list(zip(*rows, strict=True)) if rows else [()] * 4
        o, h, lo, c = (np.array(cols[i], float) for i in range(4))
        ml = np.array(cols[4], float) if rows and len(rows[0]) > 4 else lo
        mh = np.array(cols[5], float) if rows and len(rows[0]) > 5 else h
        t = start + np.arange(n, dtype=np.int64) * MIN
        return MinuteBars(t, o, h, lo, c, mh, ml, c, 0)


class FakeFunding:
    def __init__(self, events=()):
        self.events = list(events)

    def settlements(self, symbol, start, end):
        return [(t, r) for t, r in self.events if start <= t < end]


def flat(price: float, n: int = 60) -> list[tuple]:
    return [(price, price, price, price)] * n


def entry(side=Side.LONG, stop=98.0, tp1=103.0, tp2=106.0, qty=1.0, tp1_qty=0.4, single=False):
    sig = Signal("sig", "ETHUSDT", side, "B", H0 - 1, 100.0, stop, tp1, tp2, 1, "z")
    size = Size(qty, qty * 100, tp1_qty, single, 0.0)
    return PendingEntry(sig, size, SLIP, 1.0, 1.0, True)


def run(hours, pending, funding=()):
    b = Broker(1000.0, FEE, 5)
    b.submit(pending)
    mins = FakeMinutes(hours)
    for h in sorted(hours):
        b.process_window(h, mins, FakeFunding(funding), HOUR)
    return b


def test_entry_fills_at_next_open_with_slippage_and_fee():
    b = run({H0: flat(100.0)}, entry())
    sp = b.positions["ETHUSDT"]
    assert sp.pos.entry == pytest.approx(100.1) and sp.fees == pytest.approx(100.1 * FEE)
    assert b.cash == pytest.approx(1000 - 100.1 * FEE)


def test_stop_fills_with_double_slippage():
    b = run({H0: flat(100.0), H0 + HOUR: [(99, 99, 97, 97.5)]}, entry())
    tr = b.closed[0]
    assert tr["exit_reason"] == "stop"
    assert b.fills[-1]["price"] == pytest.approx(98 * (1 - 2 * SLIP))
    assert tr["r_mult"] < -1  # costs push the loss past 1R


def test_gap_through_stop_fills_at_the_open():
    b = run({H0: flat(100.0), H0 + HOUR: [(95, 95.5, 94, 95)]}, entry())
    assert b.fills[-1]["price"] == pytest.approx(95 * (1 - 2 * SLIP))


def test_tp1_partial_moves_stop_to_breakeven_then_stops_out():
    hours = {
        H0: flat(100.0),
        H0 + HOUR: [(101, 103.2, 101, 103)],
        H0 + 2 * HOUR: [(102, 102, 99, 99.5)],
    }
    b = run(hours, entry())
    tr = b.closed[0]
    assert tr["exit_legs"] == "tp1,stop_after_tp1"
    assert b.fills[1]["qty"] == pytest.approx(0.4) and b.fills[1]["price"] == 103.0
    be = 100.1 * (1 + 2 * FEE)
    assert b.fills[2]["price"] == pytest.approx(be * (1 - 2 * SLIP))


def test_tp1_and_tp2_in_one_bar():
    b = run({H0: flat(100.0), H0 + HOUR: [(101, 107, 101, 106.5)]}, entry())
    assert b.closed[0]["exit_legs"] == "tp1,tp2"
    assert b.closed[0]["mfe_r"] == pytest.approx((107 - 100.1) / 2.1)


def test_single_tp_closes_everything_at_tp1():
    b = run({H0: flat(100.0), H0 + HOUR: [(101, 103.5, 101, 103)]}, entry(single=True, tp1_qty=1.0))
    assert b.closed[0]["exit_legs"] == "tp1" and not b.positions


def test_same_minute_stop_and_tp_resolve_as_stop_and_are_counted():
    b = run({H0: flat(100.0), H0 + HOUR: [(100, 104, 97, 100)]}, entry())
    assert b.closed[0]["exit_reason"] == "stop" and b.conflicts == 1


def test_mark_price_triggers_the_stop():
    # last price never touches 98, the mark does
    b = run({H0: flat(100.0), H0 + HOUR: [(99, 99.5, 98.5, 99, 97.9, 99.5)]}, entry())
    assert b.closed[0]["exit_reason"] == "stop"


def test_funding_is_paid_by_longs_on_positive_rates():
    b = run({H0: flat(100.0), H0 + HOUR: flat(100.0)}, entry(), funding=[(H0 + HOUR, 0.0001)])
    sp = b.positions["ETHUSDT"]
    assert sp.funding == pytest.approx(1.0 * 100 * 0.0001)


def test_funding_at_the_entry_instant_is_not_charged():
    b = run({H0: flat(100.0)}, entry(), funding=[(H0, 0.01)])
    assert b.positions["ETHUSDT"].funding == 0.0


def test_entry_beyond_stop_is_skipped():
    b = run({H0: flat(97.0)}, entry())
    assert not b.positions and b.skipped[0][1] == "gap_through_stop"


def test_short_mirror():
    hours = {H0: flat(100.0), H0 + HOUR: [(99, 99, 96.8, 97)]}
    b = run(hours, entry(side=Side.SHORT, stop=102, tp1=97, tp2=94))
    assert b.fills[1]["leg"] == "tp1" and b.fills[1]["side"] == "buy"
    assert b.positions["ETHUSDT"].pos.stop == pytest.approx(99.9 * (1 - 2 * FEE))


def test_liquidation_raises():
    # a stop placed beyond the liquidation price (the risk rules forbid this)
    hours = {H0: flat(100.0), H0 + HOUR: [(99, 99, 75, 76)]}
    with pytest.raises(LiquidationError):
        run(hours, entry(stop=70.0, tp1=130, tp2=160))


def test_metrics():
    assert profit_factor(pd.Series([2.0, -1.0, 1.0])) == 3.0
    assert math.isinf(profit_factor(pd.Series([1.0])))
    eq = pd.DataFrame({"time": [0, 1, 2, 3], "equity": [100, 120, 90, 130]})
    assert max_drawdown(eq) == pytest.approx(0.25)
