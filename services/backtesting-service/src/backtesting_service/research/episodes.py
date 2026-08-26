"""Session-day episodes: the opening range, its context, and the forward path.

S1, S2, S3 and S9 all need the same unit of observation — one session anchor on one
day, its opening range, and the bars that follow. This module rebuilds that unit from
the same primitives the engine uses (``anchors.bars_in_orb_window``, ``entry_time``)
and mirrors the engine's weekday, anchor-drift and doji rules, so an episode exists
exactly where the engine would have produced a signal. A test asserts that agreement.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..anchors import (
    SessionAnchor,
    bar_open_ts,
    bars_in_orb_window,
    drift_minutes,
    is_anchor_weekday,
    session_anchor_ts,
)
from ..models import Candle, EngineParams

ATR_PERIOD = 14


@dataclass(frozen=True, slots=True)
class Episode:
    """One session anchor on one day, as the engine would have seen it."""

    session: str
    anchor_ts: datetime
    signal_ts: datetime
    entry_ts: datetime
    orb_high: float
    orb_low: float
    orb_range_price: float
    orb_range_pips: float
    orb_bar_count: int
    orb_volume: float
    bullish: bool
    anchor_drift_minutes: float
    entry_index: int | None
    entry_price: float | None
    atr_pips: float | None
    prior_range_price: float | None
    prior_volume: float | None

    @property
    def range_expansion(self) -> float | None:
        if not self.prior_range_price:
            return None
        return self.orb_range_price / self.prior_range_price

    @property
    def volume_expansion(self) -> float | None:
        if not self.prior_volume:
            return None
        return self.orb_volume / self.prior_volume


def build_episodes(
    candles: list[Candle],
    anchors: list[SessionAnchor],
    params: EngineParams,
    *,
    atr_period: int = ATR_PERIOD,
) -> list[Episode]:
    """Every session-day the engine would have signalled on, in chronological order."""
    if not candles:
        return []
    opens = [bar_open_ts(candle, params.timeframe_minutes) for candle in candles]
    atr_series = atr_pips_series(candles, params, period=atr_period)
    episodes: list[Episode] = []
    for anchor in anchors:
        for anchor_ts in _anchor_instants(candles, anchor, params):
            episode = _episode(
                candles,
                opens,
                atr_series,
                anchor=anchor,
                anchor_ts=anchor_ts,
                params=params,
            )
            if episode is not None:
                episodes.append(episode)
    episodes.sort(key=lambda item: (item.anchor_ts, item.session))
    return episodes


def _anchor_instants(
    candles: list[Candle], anchor: SessionAnchor, params: EngineParams
) -> list[datetime]:
    seen: dict[datetime, None] = {}
    span = timedelta(minutes=params.timeframe_minutes)
    for candle in candles:
        for moment in (candle.ts - span, candle.ts):
            if not is_anchor_weekday(anchor, moment):
                continue
            seen.setdefault(session_anchor_ts(anchor, moment), None)
    return sorted(seen)


def _episode(
    candles: list[Candle],
    opens: list[datetime],
    atr_series: list[float | None],
    *,
    anchor: SessionAnchor,
    anchor_ts: datetime,
    params: EngineParams,
) -> Episode | None:
    orb_bars = bars_in_orb_window(
        candles,
        timeframe_minutes=params.timeframe_minutes,
        anchor_ts=anchor_ts,
        orb_minutes=params.orb_minutes,
    )
    if not orb_bars:
        return None
    first_open_ts = bar_open_ts(orb_bars[0], params.timeframe_minutes)
    drift = drift_minutes(first_open_ts, anchor_ts)
    if drift > params.anchor_tolerance_minutes:
        return None
    orb_end = anchor_ts + timedelta(minutes=params.orb_minutes)
    if orb_bars[-1].ts < orb_end:
        # The engine only completes an opening range once a bar closes at or after its
        # end. A window truncated by the data's edge never produces a signal.
        return None
    high = max(bar.high for bar in orb_bars)
    low = min(bar.low for bar in orb_bars)
    range_price = high - low
    if range_price <= 0:
        return None
    if params.skip_doji and orb_bars[-1].close == orb_bars[0].open:
        return None

    signal_ts = orb_bars[-1].ts
    entry_ts = max(orb_end, anchor_ts + timedelta(minutes=params.entry_delay_minutes))
    entry_index = _first_bar_from(opens, entry_ts)
    signal_index = bisect_left([candle.ts for candle in candles], signal_ts)
    prior_range, prior_volume = _prior_window(candles, params, anchor_ts=anchor_ts)

    return Episode(
        session=anchor.name,
        anchor_ts=anchor_ts,
        signal_ts=signal_ts,
        entry_ts=entry_ts,
        orb_high=high,
        orb_low=low,
        orb_range_price=range_price,
        orb_range_pips=range_price / params.pip_size,
        orb_bar_count=len(orb_bars),
        orb_volume=sum(bar.volume for bar in orb_bars),
        bullish=orb_bars[-1].close > orb_bars[0].open,
        anchor_drift_minutes=drift,
        entry_index=entry_index,
        entry_price=candles[entry_index].open if entry_index is not None else None,
        atr_pips=atr_series[signal_index] if signal_index < len(atr_series) else None,
        prior_range_price=prior_range,
        prior_volume=prior_volume,
    )


def _first_bar_from(opens: list[datetime], moment: datetime) -> int | None:
    index = bisect_left(opens, moment)
    return index if index < len(opens) else None


def _prior_window(
    candles: list[Candle], params: EngineParams, *, anchor_ts: datetime
) -> tuple[float | None, float | None]:
    """The equal-length window immediately before the anchor, for expansion ratios."""
    start = anchor_ts - timedelta(minutes=params.orb_minutes)
    bars = [
        candle
        for candle in candles
        if start <= bar_open_ts(candle, params.timeframe_minutes) < anchor_ts
    ]
    if not bars:
        return None, None
    span = max(bar.high for bar in bars) - min(bar.low for bar in bars)
    return span, sum(bar.volume for bar in bars)


def atr_pips_series(
    candles: list[Candle], params: EngineParams, *, period: int = ATR_PERIOD
) -> list[float | None]:
    """Wilder true range averaged over ``period`` completed bars, in pips."""
    true_ranges: list[float] = []
    previous_close: float | None = None
    for candle in candles:
        if previous_close is None:
            true_ranges.append(candle.high - candle.low)
        else:
            true_ranges.append(
                max(
                    candle.high - candle.low,
                    abs(candle.high - previous_close),
                    abs(candle.low - previous_close),
                )
            )
        previous_close = candle.close
    series: list[float | None] = []
    for index in range(len(candles)):
        if index + 1 < period:
            series.append(None)
            continue
        window = true_ranges[index + 1 - period : index + 1]
        series.append(sum(window) / period / params.pip_size)
    return series


def tercile_edges(values: list[float]) -> tuple[float, float] | None:
    """Lower and upper tercile cut points, or None when there is nothing to split."""
    ordered = sorted(values)
    if len(ordered) < 3:
        return None
    return _quantile(ordered, 1 / 3), _quantile(ordered, 2 / 3)


def tercile_label(value: float | None, edges: tuple[float, float] | None) -> str:
    if value is None or edges is None:
        return "unclassified"
    if value <= edges[0]:
        return "low"
    if value <= edges[1]:
        return "mid"
    return "high"


def _quantile(ordered: list[float], fraction: float) -> float:
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight
