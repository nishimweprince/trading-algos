"""Per-bar precomputation: causal context, forward outcome, chart shape.

One row per closed candle, independent of any marked trade. Where `occurrences`
answers "when I took setup X in context C, what happened", this answers "in
context C, what did price do next" — the base rate the setup's edge is measured
against.

The row is assembled from halves that never see each other's bars: `context_half`
and `tags_half` read only bars at or before the anchor, `forward_half` reads only
bars after it. Leakage is a structural impossibility here rather than something a
test has to catch after the fact.
"""

from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import pandas as pd

from app.config import settings
from app.services.context import (
    atr_bucket,
    compute_context,
    context_fingerprint,
    session_from_ts,
    swing_indices,
)
from app.taggers import tag_bar
from app.utils.time import to_utc_series

# Bars scored forward of the anchor. The touch scan and the stored forward shape
# both run to this depth; the per-horizon excursion columns slice into it.
def max_horizon() -> int:
    return max(settings.feature_horizons)


def horizon_columns(horizon: int) -> list[str]:
    """Column names produced for one forward horizon, in write order."""
    h = horizon
    return [
        f"fwd{h}_max_pips",
        f"fwd{h}_min_pips",
        f"fwd{h}_max_atr",
        f"fwd{h}_min_atr",
        f"fwd{h}_bars_to_max",
        f"fwd{h}_bars_to_min",
        f"fwd{h}_max_first",
        f"fwd{h}_close_pips",
        f"fwd{h}_close_atr",
        f"fwd{h}_bars_available",
        f"fwd{h}_complete",
    ]


def level_key(level: float) -> str:
    """Touch-level keys, formatted the way the labeler formats r_grid keys."""
    return f"{level:.1f}"


# --------------------------------------------------------------------------
# Context half — bars at or before the anchor only
# --------------------------------------------------------------------------


def _efficiency_ratio(close: pd.Series, lookback: int) -> float | None:
    """Kaufman efficiency ratio: net movement over path travelled.

    Near 1 is a clean directional run, near 0 is chop. ATR says how big the bars
    are; this says whether they are going anywhere, which nothing else here
    measures.
    """
    if len(close) <= lookback:
        return None
    seg = close.iloc[-lookback - 1 :]
    path = float(seg.diff().abs().sum())
    if not path:
        return None
    return abs(float(seg.iloc[-1]) - float(seg.iloc[0])) / path


def _close_range_pct(window: pd.DataFrame, lookback: int) -> float | None:
    """Where the close sits in the trailing range: 0 at the low, 1 at the high."""
    seg = window.iloc[-lookback:]
    high = float(seg["high"].max())
    low = float(seg["low"].min())
    span = high - low
    if span <= 0:
        return None
    return (float(window["close"].iloc[-1]) - low) / span


def _realized_vol_atr(close: pd.Series, lookback: int, atr_val: float) -> float | None:
    """Return dispersion in ATR units.

    Two windows can share an ATR while one trends smoothly and the other gaps
    around; scaled against ATR, this separates them.
    """
    if len(close) <= lookback or not atr_val:
        return None
    returns = close.pct_change().iloc[-lookback:]
    sd = float(returns.std())
    if math.isnan(sd):
        return None
    return sd * float(close.iloc[-1]) / atr_val


def _swing_distances(window: pd.DataFrame, lookback: int, close0: float, atr_val: float) -> dict:
    """Distance to the last confirmed swings, complementing bars-since."""
    empty = {"dist_swing_high_atr": None, "dist_swing_low_atr": None}
    if not atr_val:
        return empty
    high_idx, low_idx = swing_indices(window, lookback)
    return {
        "dist_swing_high_atr": (float(window["high"].iloc[high_idx]) - close0) / atr_val
        if high_idx is not None
        else None,
        "dist_swing_low_atr": (close0 - float(window["low"].iloc[low_idx])) / atr_val
        if low_idx is not None
        else None,
    }


def _round_number_dist_atr(close0: float, pip_size: float, atr_val: float) -> float | None:
    """Distance to the nearest big figure, in ATR units.

    A hundred pips is the big-figure grid on every instrument the pip table
    covers: 10.0 on XAUUSD, 0.0100 on EURUSD, 1.00 on the JPY crosses.
    """
    if not atr_val or not pip_size:
        return None
    step = pip_size * 100
    return abs(close0 - round(close0 / step) * step) / atr_val


def _period_key_day(ts) -> tuple:
    ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    return (ts.year, ts.month, ts.day)


def _period_key_week(ts) -> tuple:
    ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    iso = ts.isocalendar()
    return (iso[0], iso[1])


def _prior_period_distances(
    window: pd.DataFrame,
    key_fn,
    close0: float,
    atr_val: float,
    prefix: str,
) -> dict:
    """High/low of the most recent completed day or week, in ATR units."""
    keys = [f"{prefix}_high_dist_atr", f"{prefix}_low_dist_atr"]
    if not atr_val:
        return dict.fromkeys(keys)

    period = window["ts"].map(key_fn)
    current = period.iloc[-1]
    prior_mask = period != current
    if not prior_mask.any():
        return dict.fromkeys(keys)

    last_prior = period[prior_mask].iloc[-1]
    seg = window[period == last_prior]
    return {
        keys[0]: (float(seg["high"].max()) - close0) / atr_val,
        keys[1]: (close0 - float(seg["low"].min())) / atr_val,
    }


def _session_position(window: pd.DataFrame, ts: datetime) -> dict:
    """How far into its session the anchor bar sits, and whether it overlaps.

    The three-band split folds the London/NY overlap into "ny", which is the most
    active regime of the day and behaves nothing like the rest of the block.
    """
    current = session_from_ts(ts)
    day = _period_key_day(ts)
    count = 0
    for raw in reversed(window["ts"].tolist()):
        bar_ts = raw.to_pydatetime() if hasattr(raw, "to_pydatetime") else raw
        if _period_key_day(bar_ts) != day or session_from_ts(bar_ts) != current:
            break
        count += 1
    return {
        "bar_in_session": count - 1,
        "session_overlap": settings.session_overlap_start
        <= ts.hour
        < settings.session_overlap_end,
    }


def _volume_z(window: pd.DataFrame, lookback: int) -> float | None:
    """Activity relative to the recent norm. HistData volume is tick count, which
    is not traded size but is a real measure of how busy the bar was."""
    if "volume" not in window.columns or len(window) <= lookback:
        return None
    seg = window["volume"].iloc[-lookback:].astype(float)
    sd = float(seg.std())
    if math.isnan(sd) or sd == 0:
        return None
    return (float(window["volume"].iloc[-1]) - float(seg.mean())) / sd


def context_half(
    window: pd.DataFrame,
    symbol: str,
    timeframe: str,
    pip_size: float,
    htf: dict | None = None,
) -> dict:
    """Every feature derivable from bars at or before the anchor.

    `window` ends at the anchor bar and is sliced by the caller to exactly the
    bars `fetch_labeling_window` would have returned, so `compute_context` sees
    the same input here as it does on the live path.
    """
    htf = htf or {}
    window = window.copy()
    window["ts"] = to_utc_series(window["ts"])
    anchor_idx = len(window) - 1
    ctx = compute_context(window, anchor_idx, htf_trend_state=htf.get("trend_state"))

    close0 = float(window["close"].iloc[-1])
    atr_val = float(ctx["atr_at_signal"] or 0.0)
    ts = window["ts"].iloc[-1]
    ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts

    row: dict = dict(ctx)
    row.pop("atr_terciles", None)

    row.update(
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "ts": ts,
            "close": close0,
            "atr_at_bar": atr_val or None,
            "pip_size": pip_size,
            "back_bars_available": min(len(window), settings.shape_back_bars),
            "context_fingerprint": context_fingerprint(None, symbol, timeframe, ctx),
            "efficiency_ratio": _efficiency_ratio(
                window["close"], settings.efficiency_ratio_lookback
            ),
            "close_range_pct": _close_range_pct(window, settings.close_range_lookback),
            "realized_vol_atr": _realized_vol_atr(
                window["close"], settings.close_range_lookback, atr_val
            ),
            "round_number_dist_atr": _round_number_dist_atr(close0, pip_size, atr_val),
            "volume_z": _volume_z(window, settings.volume_z_lookback),
            "htf_atr_bucket": htf.get("atr_bucket"),
            "htf_range_pct": htf.get("range_pct"),
        }
    )
    row.update(_swing_distances(window, settings.swing_lookback, close0, atr_val))
    row.update(_prior_period_distances(window, _period_key_day, close0, atr_val, "prior_day"))
    row.update(_prior_period_distances(window, _period_key_week, close0, atr_val, "prior_week"))
    row.update(_session_position(window, ts))

    shape = normalize_shape(window, close0, atr_val, settings.shape_back_bars)
    row["shape_480"] = shape
    row["shape_48"] = downsample_shape(shape, settings.shape_downsample_groups)
    return row


# --------------------------------------------------------------------------
# Tags half — bars at or before the anchor only
# --------------------------------------------------------------------------


def tags_half(window: pd.DataFrame, atr_val: float) -> dict:
    """Which patterns are present on the anchor bar.

    Same rule as the context half: bars at or before the anchor only. `forward`
    is not a parameter here and is not in scope, so a tagger cannot read past the
    anchor even by accident — which is why this lives beside the other halves
    rather than in the builder loop, where the forward frame is a local variable.

    `tag_setup_ids` denormalises the JSON so DuckDB can filter on a tag without
    parsing it, and `tag_primary_setup_id` is the single label the UI shows. Both
    stay non-null (empty string, not NULL) so a month with no tagged bar still
    writes a VARCHAR column rather than a null-typed one.
    """
    result = tag_bar(window, atr_val)
    return {
        "bar_tags": result.to_json(),
        "tag_setup_ids": ",".join(result.setup_ids()),
        "tag_primary_setup_id": result.primary_setup_id(),
        "tag_count": len(result.tags),
    }


# --------------------------------------------------------------------------
# Forward half — bars after the anchor only
# --------------------------------------------------------------------------


def forward_excursions(
    forward: pd.DataFrame,
    close0: float,
    atr_val: float,
    pip_size: float,
    horizons: list[int],
) -> dict:
    """Highest and lowest price reached in each forward horizon.

    Both are signed against the anchor close, and `max_first` records which came
    first — without the ordering the same two extremes describe a 1:1 winner and
    a 1:1 loser equally well.
    """
    out: dict = {}
    for h in horizons:
        seg = forward.iloc[:h]
        available = len(seg)
        cols = dict.fromkeys(horizon_columns(h))
        cols[f"fwd{h}_bars_available"] = available
        cols[f"fwd{h}_complete"] = available >= h

        if available:
            highs = seg["high"].to_numpy(dtype=float)
            lows = seg["low"].to_numpy(dtype=float)
            max_i = int(np.argmax(highs))
            min_i = int(np.argmin(lows))
            max_delta = float(highs[max_i]) - close0
            min_delta = float(lows[min_i]) - close0
            close_delta = float(seg["close"].iloc[-1]) - close0

            cols.update(
                {
                    f"fwd{h}_max_pips": max_delta / pip_size if pip_size else None,
                    f"fwd{h}_min_pips": min_delta / pip_size if pip_size else None,
                    f"fwd{h}_max_atr": max_delta / atr_val if atr_val else None,
                    f"fwd{h}_min_atr": min_delta / atr_val if atr_val else None,
                    f"fwd{h}_bars_to_max": max_i + 1,
                    f"fwd{h}_bars_to_min": min_i + 1,
                    f"fwd{h}_max_first": max_i < min_i,
                    f"fwd{h}_close_pips": close_delta / pip_size if pip_size else None,
                    f"fwd{h}_close_atr": close_delta / atr_val if atr_val else None,
                }
            )
        out.update(cols)
    return out


def forward_touch_levels(
    forward: pd.DataFrame,
    close0: float,
    atr_val: float,
    levels: list[float],
    depth: int,
) -> dict:
    """Bars until price first reaches each ATR distance, up and down.

    Storing first-touch bar numbers instead of a fixed outcome grid keeps every
    (target, stop, horizon, side) combination answerable by comparing two
    integers, and costs two numbers per level instead of one row per combination.
    """
    grid = {level_key(m): {"up": None, "down": None} for m in levels}
    if not atr_val or forward.empty:
        return grid

    seg = forward.iloc[:depth]
    highs = seg["high"].to_numpy(dtype=float)
    lows = seg["low"].to_numpy(dtype=float)
    pending = {level_key(m): (close0 + m * atr_val, close0 - m * atr_val) for m in levels}

    for i in range(len(seg)):
        for key, (up_target, down_target) in pending.items():
            entry = grid[key]
            if entry["up"] is None and highs[i] >= up_target:
                entry["up"] = i + 1
            if entry["down"] is None and lows[i] <= down_target:
                entry["down"] = i + 1
        if all(e["up"] is not None and e["down"] is not None for e in grid.values()):
            break
    return grid


def forward_half(
    forward: pd.DataFrame,
    close0: float,
    atr_val: float,
    pip_size: float,
) -> dict:
    """Every feature derivable from bars after the anchor."""
    depth = max_horizon()
    row = forward_excursions(forward, close0, atr_val, pip_size, settings.feature_horizons)
    row["level_touch"] = forward_touch_levels(
        forward, close0, atr_val, settings.touch_levels, depth
    )
    row["fwd_shape"] = normalize_shape(
        forward.iloc[:depth], close0, atr_val, depth, pad_front=False
    )
    return row


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


def normalize_shape(
    bars: pd.DataFrame,
    close0: float,
    atr_val: float,
    length: int,
    pad_front: bool = True,
) -> list[float]:
    """Flatten bars to ATR-normalised offsets from the anchor close.

    Four values per bar in OHLC order. Raw prices would make gold at 2,000 and
    gold at 3,300 incomparable, so everything is expressed relative to the anchor
    close and divided by the anchor ATR — which is also what lets two symbols be
    compared later. Wicks are kept: sweeps and rejections are wick events, and a
    body-only series erases them.

    Missing bars are NaN-padded at the front (back window, so the anchor stays at
    the end) or the back (forward window, so bar 1 stays at the start).
    """
    if not atr_val:
        return [float("nan")] * (length * 4)

    seg = bars.iloc[-length:] if pad_front else bars.iloc[:length]
    values: list[float] = []
    for row in seg[["open", "high", "low", "close"]].to_numpy(dtype=float):
        values.extend(float((v - close0) / atr_val) for v in row)

    pad = [float("nan")] * ((length - len(seg)) * 4)
    return pad + values if pad_front else values + pad


def downsample_shape(shape: list[float], groups: int) -> list[float]:
    """Compress a shape vector to `groups` bars: first open, max high, min low,
    last close per chunk. Small enough to index and compare, coarse enough that
    one noisy bar does not dominate the distance."""
    bars = np.array(shape, dtype=float).reshape(-1, 4)
    chunks = np.array_split(bars, groups)
    out: list[float] = []
    for chunk in chunks:
        valid = chunk[~np.isnan(chunk).any(axis=1)]
        if len(valid) == 0:
            out.extend([float("nan")] * 4)
            continue
        out.extend(
            [
                float(valid[0, 0]),
                float(valid[:, 1].max()),
                float(valid[:, 2].min()),
                float(valid[-1, 3]),
            ]
        )
    return out


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def compute_bar_row(
    window: pd.DataFrame,
    forward: pd.DataFrame,
    symbol: str,
    timeframe: str,
    pip_size: float,
    htf: dict | None = None,
) -> dict:
    """One feature row. `window` ends at the anchor bar; `forward` starts after it."""
    row = context_half(window, symbol, timeframe, pip_size, htf)
    # Before the forward half, so that reading forward data into a tag would take
    # moving this line rather than only adding one.
    row.update(tags_half(window, float(row["atr_at_bar"] or 0.0)))
    row.update(
        forward_half(
            forward,
            close0=row["close"],
            atr_val=float(row["atr_at_bar"] or 0.0),
            pip_size=pip_size,
        )
    )
    row["next_open"] = float(forward["open"].iloc[0]) if len(forward) else None
    row["bar_feature_version"] = settings.bar_feature_version
    row["feature_version"] = settings.feature_version
    return row


def compute_htf_context(con, symbol: str, timeframe: str, signal_ts: datetime) -> dict:
    """HTF context at one moment, for callers that have a connection and a bar.

    The builder computes this once per HTF bar and joins; this is the single-bar
    path the API uses, and both end up in `htf_context` so they cannot drift.
    """
    empty = {"trend_state": None, "atr_bucket": None, "range_pct": None}
    htf = settings.htf_map.get(timeframe)
    if not htf:
        return empty

    from app.services.candles import fetch_labeling_window

    try:
        candles, signal_idx = fetch_labeling_window(
            con, symbol, htf, signal_ts, warmup_bars=settings.warmup_bars, forward_bars=0
        )
    except ValueError:
        return empty
    return htf_context(candles.iloc[: signal_idx + 1])


def htf_context(window: pd.DataFrame) -> dict:
    """Higher-timeframe context at one HTF bar.

    `trend_state` reproduces `compute_htf_trend_state` exactly — same EMA over the
    same warmup slice — so a precomputed row and a live `/context` call agree.
    """
    from app.services.context import _atr, _ema

    close = window["close"]
    ema = _ema(close, settings.ema_period)
    ema_val = float(ema.iloc[-1]) if not pd.isna(ema.iloc[-1]) else float(close.iloc[-1])
    signal_close = float(close.iloc[-1])

    atr_series = _atr(window, settings.atr_period)
    atr_val = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 0.0
    pcts = (atr_series / close).dropna().tolist()
    terciles = (
        float(np.percentile(pcts, 33)) if pcts else 0.0,
        float(np.percentile(pcts, 67)) if pcts else 0.0,
    )

    return {
        "trend_state": "up" if signal_close > ema_val else "down",
        "atr_bucket": atr_bucket(atr_val / signal_close if signal_close else 0.0, terciles),
        "range_pct": _close_range_pct(window, settings.close_range_lookback),
    }
