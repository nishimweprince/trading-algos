"""Single-topic Phase 3 entry filters. All default off; combinations are not experiment lanes."""

from __future__ import annotations

from datetime import UTC, date, datetime

from models import Candle

D1_EMA_PERIOD = 50
NR7_LOOKBACK = 7
ORB_ATR_MIN = 0.5
ORB_ATR_MAX = 2.0
D1_CLOSE_HISTORY = 64


def utc_day(ts: datetime) -> date:
    return ts.astimezone(UTC).date()


def ema(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    seed = sum(values[:period]) / period
    k = 2.0 / (period + 1)
    current = seed
    for value in values[period:]:
        current = value * k + current * (1.0 - k)
    return current


def d1_direction_allows(*, bullish: bool, prior_close: float, ema50: float) -> bool:
    if prior_close == ema50:
        return False
    aligned_long = prior_close > ema50
    return aligned_long if bullish else not aligned_long


def is_nr7(ranges: list[float], *, lookback: int = NR7_LOOKBACK) -> bool | None:
    """True when the latest range is the narrowest of the last ``lookback`` same-session ORBs."""
    if len(ranges) < lookback:
        return None
    window = ranges[-lookback:]
    return window[-1] <= min(window)


def orb_atr_ratio(range_price: float, atr: float) -> float | None:
    if atr <= 0 or range_price < 0:
        return None
    return range_price / atr


class DailyCloseTracker:
    """Completed UTC D1 closes from parent bars. The in-progress day is not a close."""

    def __init__(self) -> None:
        self.closes: list[float] = []
        self.open_day: date | None = None
        self.open_close: float | None = None
        self._last_ts: datetime | None = None

    def observe(self, bar: Candle) -> None:
        if self._last_ts is not None and bar.ts == self._last_ts:
            return
        self._last_ts = bar.ts
        day = utc_day(bar.ts)
        if self.open_day is None:
            self.open_day = day
            self.open_close = bar.close
            return
        if day != self.open_day:
            if self.open_close is not None:
                self.closes.append(self.open_close)
                overflow = len(self.closes) - D1_CLOSE_HISTORY
                if overflow > 0:
                    del self.closes[:overflow]
            self.open_day = day
            self.open_close = bar.close
            return
        self.open_close = bar.close

    def prior_close_and_ema(self, period: int = D1_EMA_PERIOD) -> tuple[float, float] | None:
        value = ema(self.closes, period)
        if value is None:
            return None
        return self.closes[-1], value

    def snapshot(self) -> dict[str, object]:
        return {
            "closes": list(self.closes),
            "open_day": self.open_day.isoformat() if self.open_day is not None else None,
            "open_close": self.open_close,
            "last_ts": self._last_ts.isoformat() if self._last_ts is not None else None,
        }

    def restore(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        closes = payload.get("closes")
        self.closes = [float(v) for v in closes] if isinstance(closes, list) else []
        open_day = payload.get("open_day")
        self.open_day = date.fromisoformat(str(open_day)) if open_day else None
        open_close = payload.get("open_close")
        self.open_close = float(open_close) if open_close is not None else None
        last_ts = payload.get("last_ts")
        self._last_ts = datetime.fromisoformat(str(last_ts)) if last_ts else None


def parse_hours(text: str) -> frozenset[int]:
    """Parse a comma-separated UTC hour list. Empty means the filter is off."""
    hours: set[int] = set()
    for token in text.split(","):
        item = token.strip()
        if not item:
            continue
        hour = int(item)
        if not 0 <= hour <= 23:
            raise ValueError(f"entry hour must be 0-23, got {hour}")
        hours.add(hour)
    return frozenset(hours)


def entry_hour_blocked(ts: datetime, excluded: frozenset[int]) -> bool:
    """True when this structure's UTC entry hour is on the exclusion list.

    Caution: a session anchored in local time lands on two different UTC hours
    across a daylight-saving boundary. Excluding one of them excludes a *season*,
    not a time of day. Confirm the local-time mapping before using this.
    """
    if not excluded:
        return False
    return ts.astimezone(UTC).hour in excluded


def entry_filter_reason(
    *,
    filter_d1_ema50: bool,
    filter_nr7: bool,
    filter_orb_atr_min: float,
    filter_orb_atr_max: float,
    entry_hours_utc_exclude: frozenset[int],
    ts: datetime,
    bullish: bool,
    range_price: float,
    session_orb_ranges: list[float],
    prior_d1: tuple[float, float] | None,
    atr: float | None,
) -> str | None:
    """Return a skip reason, or None to allow the structure. Disabled filters are no-ops."""
    if entry_hour_blocked(ts, entry_hours_utc_exclude):
        return "filter_entry_hour"
    if (
        not filter_d1_ema50
        and not filter_nr7
        and filter_orb_atr_min <= 0
        and filter_orb_atr_max <= 0
    ):
        return None
    if filter_d1_ema50:
        if prior_d1 is None:
            return "insufficient_d1"
        prior_close, ema50 = prior_d1
        if not d1_direction_allows(bullish=bullish, prior_close=prior_close, ema50=ema50):
            return "filter_d1_ema50"
    if filter_nr7:
        nr7 = is_nr7(session_orb_ranges)
        if nr7 is None:
            return "insufficient_nr7"
        if not nr7:
            return "filter_nr7"
    if filter_orb_atr_min > 0 or filter_orb_atr_max > 0:
        if atr is None:
            return "insufficient_atr"
        ratio = orb_atr_ratio(range_price, atr)
        if ratio is None:
            return "insufficient_atr"
        if filter_orb_atr_min > 0 and ratio < filter_orb_atr_min:
            return "filter_orb_atr_min"
        if filter_orb_atr_max > 0 and ratio > filter_orb_atr_max:
            return "filter_orb_atr_max"
    return None
