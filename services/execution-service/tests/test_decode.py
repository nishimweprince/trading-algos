from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from ta_contracts import Tick

from execution_service.ctrader.decode import (
    PERIOD_SECONDS,
    decode_spot,
    decode_trendbar,
    decode_trendbars,
    scale_price,
)
from execution_service.ctrader.proto import ProtoOASpotEvent, ProtoOATrendbar


def _spot(**kwargs: int) -> ProtoOASpotEvent:
    return ProtoOASpotEvent(ctidTraderAccountId=1, symbolId=1, **kwargs)


def _bar(**kwargs: int) -> ProtoOATrendbar:
    payload = {"volume": 100, "low": 110000, "deltaOpen": 200, "deltaHigh": 500, "deltaClose": 100}
    payload.update(kwargs)
    return ProtoOATrendbar(**payload)


# --- scaling -----------------------------------------------------------------


def test_scale_price_divides_by_1e5_and_rounds_to_digits() -> None:
    assert scale_price(108532, 5) == 1.08532
    assert scale_price(330012, 2) == 3.30


def test_rounding_removes_float_noise() -> None:
    """Without the round, this is 1.1000000000000001 and leaks into every payload."""
    assert repr(scale_price(110000, 5)) == "1.1"


# --- spot events -------------------------------------------------------------


def test_spot_with_both_sides() -> None:
    tick = decode_spot(_spot(bid=108532, ask=108545), symbol="EURUSD", digits=5)
    assert tick is not None
    assert (tick.bid, tick.ask) == (1.08532, 1.08545)
    assert tick.spread == pytest.approx(0.00013)


def test_spot_with_only_bid_merges_the_cached_ask() -> None:
    """cTrader sends only the side that changed. Reading event.ask without
    HasField would yield 0.0 here."""
    previous = Tick(symbol="EURUSD", bid=1.08530, ask=1.08545, spread=0.00015, ts=datetime.now(UTC))

    tick = decode_spot(_spot(bid=108532), symbol="EURUSD", digits=5, previous=previous)

    assert tick is not None
    assert tick.bid == 1.08532
    assert tick.ask == 1.08545


def test_spot_with_only_ask_merges_the_cached_bid() -> None:
    previous = Tick(symbol="EURUSD", bid=1.08530, ask=1.08545, spread=0.00015, ts=datetime.now(UTC))

    tick = decode_spot(_spot(ask=108550), symbol="EURUSD", digits=5, previous=previous)

    assert tick is not None
    assert (tick.bid, tick.ask) == (1.08530, 1.08550)


def test_spot_emits_nothing_until_both_sides_are_known() -> None:
    """Half a quote is worse than no quote."""
    assert decode_spot(_spot(bid=108532), symbol="EURUSD", digits=5, previous=None) is None


def test_spot_uses_server_timestamp_when_present() -> None:
    tick = decode_spot(
        _spot(bid=108532, ask=108545, timestamp=1_700_000_000_000), symbol="EURUSD", digits=5
    )
    assert tick is not None
    assert tick.ts == datetime.fromtimestamp(1_700_000_000, UTC)


def test_spot_falls_back_to_the_clock_without_a_server_timestamp() -> None:
    fixed = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    tick = decode_spot(
        _spot(bid=108532, ask=108545), symbol="EURUSD", digits=5, clock=lambda: fixed
    )
    assert tick is not None
    assert tick.ts == fixed


def test_spot_respects_symbol_digits() -> None:
    tick = decode_spot(_spot(bid=330012, ask=330045), symbol="XAUUSD", digits=2)
    assert tick is not None
    assert (tick.bid, tick.ask) == (3.30, 3.30)


# --- trendbars ---------------------------------------------------------------


def test_trendbar_delta_decoding() -> None:
    candle = decode_trendbar(_bar(), symbol="EURUSD", period="M1", digits=5)
    assert (candle.open, candle.high, candle.low, candle.close) == (1.102, 1.105, 1.1, 1.101)
    assert candle.volume == 100.0
    assert candle.provider == "ctrader"
    assert candle.source_instrument == "EURUSD"


@pytest.mark.parametrize("period", ["M1", "M5", "M15", "H1", "H4", "D1", "W1"])
def test_timestamp_is_stamped_at_the_interval_end(period: str) -> None:
    """cTrader sends the OPEN tick's time; lookup-trader's contract is interval END."""
    start = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    bar = _bar(utcTimestampInMinutes=int(start.timestamp() // 60))

    candle = decode_trendbar(bar, symbol="EURUSD", period=period, digits=5)

    assert candle.ts == start + timedelta(seconds=PERIOD_SECONDS[period])


def test_unsupported_period_rejected() -> None:
    with pytest.raises(ValueError, match="MN1"):
        decode_trendbar(_bar(), symbol="EURUSD", period="MN1", digits=5)


def test_ohlc_invariant_violation_raises() -> None:
    """Only reachable via a bad `low` or a scaling regression — which is exactly
    what this is here to catch."""
    candle = _bar(deltaOpen=900, deltaHigh=500)
    with pytest.raises(ValueError, match="open"):
        decode_trendbar(candle, symbol="EURUSD", period="M1", digits=5)


def test_negative_volume_rejected() -> None:
    with pytest.raises(ValueError, match="volume"):
        decode_trendbar(_bar(volume=-1), symbol="EURUSD", period="M1", digits=5)


def test_forming_bar_is_dropped() -> None:
    now = datetime(2026, 8, 8, 12, 30, tzinfo=UTC)
    closed_start = int((now - timedelta(hours=1)).timestamp() // 60)
    forming_start = int(now.timestamp() // 60)

    candles = decode_trendbars(
        [_bar(utcTimestampInMinutes=closed_start), _bar(utcTimestampInMinutes=forming_start)],
        symbol="EURUSD",
        period="H1",
        digits=5,
        clock=lambda: now,
    )

    assert len(candles) == 1
    assert candles[0].ts == now


def test_decoded_bars_are_sorted_by_timestamp() -> None:
    now = datetime(2026, 8, 8, 23, 0, tzinfo=UTC)
    minutes = [int((now - timedelta(hours=h)).timestamp() // 60) for h in (1, 3, 2)]

    candles = decode_trendbars(
        [_bar(utcTimestampInMinutes=m) for m in minutes],
        symbol="EURUSD",
        period="H1",
        digits=5,
        clock=lambda: now,
    )

    assert [c.ts for c in candles] == sorted(c.ts for c in candles)
