"""Wire messages to domain models. Pure functions, no I/O.

Two conversions carry all the risk here and both are easy to get silently wrong:
price scaling, and the interval-start to interval-end timestamp shift.

UNVERIFIED against a live broker as of 2026-08-08: trendbars are assumed to be
bid-side, matching cTrader's documentation, and every candle this service serves
inherits that assumption. `tests/test_integration_demo.py` asserts it directly
but has never been run — there is no demo account configured on this machine.
Run `CTRADER_INTEGRATION=1 pytest -m integration` and record the verdict here
before trusting candles for anything that trades.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from models import Candle, Tick

# Every price on the wire is an integer in 1/100000 of a unit of price:
# 108532 means 1.08532. Documented on ProtoOASpotEvent.bid.
PRICE_SCALE = 100_000

# ProtoOATrendbarPeriod durations. MN1 is excluded on purpose — a calendar month
# has no constant length, so its interval end cannot be derived by addition.
PERIOD_SECONDS: dict[str, int] = {
    "M1": 60,
    "M2": 120,
    "M3": 180,
    "M4": 240,
    "M5": 300,
    "M10": 600,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "H12": 43200,
    "D1": 86400,
    "W1": 604800,
}

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def scale_price(raw: int, digits: int) -> float:
    """Divide by the wire scale, then round to the symbol's digits.

    Rounding after the divide matters: without it every tick carries
    1.1000000000000001-class float noise into SSE payloads and any downstream
    hashing or comparison.
    """
    return round(raw / PRICE_SCALE, digits)


def period_duration(period: str) -> timedelta:
    try:
        return timedelta(seconds=PERIOD_SECONDS[period])
    except KeyError as exc:
        raise ValueError(f"Unsupported trendbar period {period!r}") from exc


def decode_spot(
    event: object,
    *,
    symbol: str,
    digits: int,
    previous: Tick | None = None,
    clock: Clock = utc_now,
) -> Tick | None:
    """Merge a ProtoOASpotEvent against the last known quote.

    cTrader populates only the side that changed, so `bid` and `ask` are proto2
    optional fields. Reading them without HasField yields 0.0 for the unchanged
    side and produces a nonsensical quote. Returns None until both sides are
    known, rather than emitting half a tick.
    """
    has_bid = event.HasField("bid")
    has_ask = event.HasField("ask")

    bid = scale_price(event.bid, digits) if has_bid else (previous.bid if previous else None)
    ask = scale_price(event.ask, digits) if has_ask else (previous.ask if previous else None)
    if bid is None or ask is None:
        return None

    if event.HasField("timestamp"):
        ts = datetime.fromtimestamp(event.timestamp / 1000, UTC)
    else:
        ts = clock()

    return Tick(
        symbol=symbol,
        bid=bid,
        ask=ask,
        spread=round(ask - bid, digits),
        ts=ts,
    )


def decode_trendbar(bar: object, *, symbol: str, period: str, digits: int) -> Candle:
    """Reconstruct OHLC from cTrader's delta encoding.

    `low` is the only absolute price; open/high/close are unsigned deltas above
    it. `utcTimestampInMinutes` is the timestamp of the bar's *open* tick, so the
    interval end — which is what lookup-trader's Candle contract expects — is the
    start plus the period duration.
    """
    low_raw = int(bar.low)
    start = datetime.fromtimestamp(int(bar.utcTimestampInMinutes) * 60, UTC)

    candle = Candle(
        ts=start + period_duration(period),
        open=scale_price(low_raw + int(bar.deltaOpen), digits),
        high=scale_price(low_raw + int(bar.deltaHigh), digits),
        low=scale_price(low_raw, digits),
        close=scale_price(low_raw + int(bar.deltaClose), digits),
        volume=float(bar.volume),
        source_instrument=symbol,
        spread_source="unavailable",
    )
    _validate(candle)
    return candle


def _validate(candle: Candle) -> None:
    """Reject a corrupt bar rather than publishing it.

    The delta encoding is unsigned, so these can only trip on a bad `low` or a
    scaling regression — which is exactly what makes the check worth keeping.
    Mirrors the OHLC validation in lookup-trader's providers/capital.py.
    """
    if candle.high < candle.low:
        raise ValueError(f"{candle.source_instrument} {candle.ts}: high {candle.high} < low")
    for name, value in (("open", candle.open), ("close", candle.close)):
        if not candle.low <= value <= candle.high:
            raise ValueError(
                f"{candle.source_instrument} {candle.ts}: {name} {value} outside "
                f"[{candle.low}, {candle.high}]"
            )
    if candle.volume < 0:
        raise ValueError(f"{candle.source_instrument} {candle.ts}: negative volume")


def decode_trendbars(
    bars: object,
    *,
    symbol: str,
    period: str,
    digits: int,
    clock: Clock = utc_now,
) -> tuple[Candle, ...]:
    """Decode a history response, dropping the bar that is still forming.

    A response includes the current, incomplete interval. Publishing it would
    break the closed-candle contract downstream consumers rely on — the same
    reason lookup-trader's Capital client applies a settlement cutoff.
    """
    now = clock()
    decoded = [decode_trendbar(bar, symbol=symbol, period=period, digits=digits) for bar in bars]
    closed = [candle for candle in decoded if candle.ts <= now]
    return tuple(sorted(closed, key=lambda candle: candle.ts))
