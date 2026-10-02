"""The simulated venue: queue position, latency, cancel races (shadow and backtest)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from ofi_scalper_service.policy import Phase
from tests.model_fixture import Dial
from tests.test_bridge import GATE_ON, T0, S, sample, shadow_bridge

MS = 1_000_000


class Book:
    """Just what the venue reads from the plugin's LocalOrderBook."""

    def __init__(self, bids: dict[float, float], asks: dict[float, float]) -> None:
        self.bids, self.asks = bids, asks

    def top(self, levels: int):
        return (
            sorted(self.bids.items(), reverse=True)[:levels],
            sorted(self.asks.items())[:levels],
        )


def entered(tmp_path: Path, **sim):
    dial = Dial()
    bridge, risk = shadow_bridge(tmp_path, dial)
    bridge.shadow.queue_model = sim.get("queue_model", "pessimistic")
    bridge.shadow.delay_ns = sim.get("delay_ns", 0)
    book = Book({60000.0: 2.0, 59999.9: 5.0}, {60000.1: 1.0})
    bridge.on_book("BTCUSDT", book)
    dial.set(up=0.8, down=0.05)
    bridge.on_sample(sample(T0), GATE_ON)
    dial.set()
    return bridge, book


def entry(bridge):
    return next(o for o in bridge.orders.values() if o.leg == "entry")


def test_queue_model_waits_behind_the_displayed_quantity(tmp_path: Path) -> None:
    bridge, book = entered(tmp_path, queue_model="queue")
    order = entry(bridge)
    assert bridge.shadow.queue_ahead[order.key] == Decimal("2.0")
    bridge.on_trade("BTCUSDT", T0 + 1, 60000.0, 1.5, True)  # sellers hit the bid
    assert order.executed == "0"
    bridge.on_trade("BTCUSDT", T0 + 2, 60000.0, 0.5, False)  # buyer aggressor: not us
    assert order.executed == "0"
    bridge.on_trade("BTCUSDT", T0 + 3, 60000.0, 0.501, True)  # 0.5 left ahead, 0.001 to us
    assert order.executed == "0.001" and not order.terminal
    bridge.on_trade("BTCUSDT", T0 + 4, 60000.0, 0.01, True)
    assert order.executed == "0.003" and order.terminal
    assert bridge.policies["BTCUSDT"].phase is Phase.OPEN


def test_a_shrinking_level_moves_us_up(tmp_path: Path) -> None:
    bridge, book = entered(tmp_path, queue_model="queue")
    order = entry(bridge)
    book.bids[60000.0] = 0.2  # cancels ahead of us
    bridge.on_book("BTCUSDT", book)
    assert bridge.shadow.queue_ahead[order.key] == Decimal("0.2")
    bridge.on_trade("BTCUSDT", T0 + 1, 60000.0, 0.203, True)
    assert order.executed == "0.003"


def test_pessimistic_ignores_trades_at_the_price(tmp_path: Path) -> None:
    bridge, _ = entered(tmp_path)
    order = entry(bridge)
    bridge.on_trade("BTCUSDT", T0 + 1, 60000.0, 100.0, True)
    assert order.executed == "0"
    bridge.on_trade("BTCUSDT", T0 + 2, 59999.9, 0.001, True)  # through
    assert order.executed == "0.003" and order.terminal


def test_latency_order_only_fills_on_trades_after_arrival(tmp_path: Path) -> None:
    bridge, book = entered(tmp_path, delay_ns=5 * MS)
    order = entry(bridge)
    assert order.key not in bridge.shadow.resting  # still on its way
    bridge.on_trade("BTCUSDT", T0 + 2 * MS, 59999.0, 1.0, True)
    assert order.executed == "0"
    bridge.on_trade("BTCUSDT", T0 + 6 * MS, 59999.0, 1.0, True)  # arrived at +5 ms, then filled
    assert order.executed == "0.003" and order.acked_ns == T0 + 5 * MS


def test_post_only_check_uses_the_touch_at_arrival(tmp_path: Path) -> None:
    bridge, book = entered(tmp_path, delay_ns=5 * MS)
    book.asks = {60000.0: 1.0}  # the ask came down to our bid meanwhile
    book.bids = {59999.9: 1.0}
    bridge.advance(T0 + 5 * MS)
    assert bridge.policies["BTCUSDT"].phase is Phase.FLAT
    assert bridge.counts["cycles_unfilled"] == 1


def test_a_cancel_can_lose_to_a_fill(tmp_path: Path) -> None:
    bridge, _ = entered(tmp_path, delay_ns=5 * MS)
    bridge.advance(T0 + 5 * MS)  # the entry rests
    order = entry(bridge)
    bridge.on_sample(sample(T0 + 1 * S), GATE_ON)  # timeout: cancel sent, arrives at +5 ms
    assert order.cancel_requested and not order.terminal
    bridge.on_trade("BTCUSDT", T0 + 1 * S + 1 * MS, 59999.0, 1.0, True)  # fill wins the race
    assert order.executed == "0.003"
    bridge.advance(T0 + 1 * S + 10 * MS)
    assert bridge.policies["BTCUSDT"].phase is Phase.OPEN


def test_market_close_fills_at_the_touch_on_arrival(tmp_path: Path) -> None:
    bridge, book = entered(tmp_path, delay_ns=5 * MS)
    bridge.advance(T0 + 5 * MS)
    bridge.on_trade("BTCUSDT", T0 + 6 * MS, 59999.0, 1.0, True)
    book.bids = {59950.0: 1.0}
    book.asks = {59950.1: 1.0}
    bridge.on_sample(sample(T0 + S, bid=59950.0, ask=59950.1), GATE_ON)  # stop
    book.bids = {59940.0: 1.0}  # it falls further before the close arrives
    bridge.advance(T0 + S + 5 * MS)
    bridge.flush_pending()
    (trade,) = bridge.book.recent_trades
    assert trade["exit_reason"] == "stop" and trade["exit_price"] == 59940.0
    assert trade["legs"]["close1"]["qty"] == "0.003"


def test_unknown_queue_model_is_refused(tmp_path: Path) -> None:
    from ofi_scalper_service.execution_bridge import ShadowVenue

    with pytest.raises(ValueError, match="queue model"):
        ShadowVenue(None, queue_model="optimistic")  # type: ignore[arg-type]
