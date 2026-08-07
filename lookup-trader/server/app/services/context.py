from __future__ import annotations

import hashlib
import json
from datetime import datetime

import numpy as np
import pandas as pd

from app.config import settings
from app.taggers.thresholds import SWING_LOOKBACK
from app.utils.time import to_utc_series


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def session_from_ts(ts: datetime) -> str:
    hour = ts.hour
    if settings.asian_start <= hour < settings.asian_end:
        return "asian"
    if settings.london_start <= hour < settings.london_end:
        return "london"
    if settings.ny_start <= hour < settings.ny_end:
        return "ny"
    return "off_hours"


def day_of_week_from_ts(ts: datetime) -> str:
    return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][ts.weekday()]


def rsi_band(rsi_val: float) -> str:
    if rsi_val < 30:
        return "oversold"
    if rsi_val > 70:
        return "overbought"
    return "neutral"


def atr_bucket(atr_pct: float, terciles: tuple[float, float]) -> str:
    low, high = terciles
    if atr_pct <= low:
        return "low"
    if atr_pct >= high:
        return "high"
    return "mid"


def _bucket(value: float | None, cuts: tuple[float, float], names: tuple[str, str, str]) -> str | None:
    """Three-way split on two cut points. Returns None so an absent value stays absent."""
    if value is None:
        return None
    low, high = cuts
    if value < low:
        return names[0]
    if value >= high:
        return names[2]
    return names[1]


def rr_bucket(rr_planned: float | None) -> str | None:
    return _bucket(rr_planned, settings.rr_buckets, ("low", "standard", "high"))


def sl_atr_bucket(sl_atr_mult: float | None) -> str | None:
    """Stop width relative to volatility — a 0.5 ATR stop and a 3 ATR stop on the
    same setup are different trades, and pooling them hides that."""
    return _bucket(sl_atr_mult, settings.sl_atr_buckets, ("tight", "normal", "wide"))


def ema_slope_bucket(slope_atr: float | None) -> str | None:
    return _bucket(slope_atr, settings.ema_slope_buckets, ("down", "flat", "up"))


def atr_change_bucket(ratio: float | None) -> str | None:
    return _bucket(ratio, settings.atr_change_buckets, ("contracting", "stable", "expanding"))


def _day_high_low(window: pd.DataFrame, ts: datetime) -> tuple[float, float]:
    """Running UTC-day high/low up to and including the signal bar."""
    day = ts.date()
    mask = window["ts"].apply(
        lambda t: (t.to_pydatetime() if hasattr(t, "to_pydatetime") else t).date() == day
    )
    day_bars = window.loc[mask]
    if day_bars.empty:
        bar = window.iloc[-1]
        return float(bar["high"]), float(bar["low"])
    return float(day_bars["high"].max()), float(day_bars["low"].min())


def _signal_candle_anatomy(bar: pd.Series, atr_val: float) -> dict:
    o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
    rng = h - l or 1e-9
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    return {
        "signal_body_pct": body / rng,
        "signal_upper_wick_pct": upper_wick / rng,
        "signal_lower_wick_pct": lower_wick / rng,
        "signal_range_atr": rng / atr_val if atr_val else None,
    }


def swing_indices(window: pd.DataFrame, lookback: int) -> tuple[int | None, int | None]:
    """Positions of the last confirmed local swing high/low in the window.

    A swing needs `lookback` bars either side to be confirmed, so the scan stops
    short of the window end — the most recent bars cannot yet be swings.
    """
    if len(window) < lookback * 2 + 1:
        return None, None

    highs = window["high"].to_numpy()
    lows = window["low"].to_numpy()
    last_high = last_low = None
    end = len(window) - 1
    for i in range(lookback, end - lookback + 1):
        if highs[i] == max(highs[i - lookback : i + lookback + 1]):
            last_high = i
        if lows[i] == min(lows[i - lookback : i + lookback + 1]):
            last_low = i

    return last_high, last_low


def _bars_since_swing(window: pd.DataFrame, lookback: int) -> dict:
    """Bars since the last local swing high/low in the trailing window."""
    last_high, last_low = swing_indices(window, lookback)
    end = len(window) - 1
    return {
        "bars_since_swing_high": end - last_high if last_high is not None else None,
        "bars_since_swing_low": end - last_low if last_low is not None else None,
    }


def compute_htf_trend_state(
    con,
    symbol: str,
    timeframe: str,
    signal_ts: datetime,
) -> str | None:
    """EMA trend on the higher timeframe at the signal moment."""
    htf = settings.htf_map.get(timeframe)
    if not htf:
        return None

    from app.services.candles import fetch_labeling_window

    try:
        candles, signal_idx = fetch_labeling_window(
            con, symbol, htf, signal_ts, warmup_bars=settings.warmup_bars, forward_bars=0
        )
    except ValueError:
        return None

    close = candles.iloc[: signal_idx + 1]["close"]
    ema = _ema(close, settings.ema_period)
    ema_val = float(ema.iloc[-1]) if not pd.isna(ema.iloc[-1]) else float(close.iloc[-1])
    signal_close = float(close.iloc[-1])
    return "up" if signal_close > ema_val else "down"


def context_fingerprint(
    setup_id: str | None,
    symbol: str,
    timeframe: str,
    ctx: dict,
) -> str:
    """Stable hash of the promoted comparison dimensions at signal time."""
    payload = {
        "setup_id": setup_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "trend_state": ctx.get("trend_state"),
        "atr_bucket": ctx.get("atr_bucket"),
        "session": ctx.get("session"),
        "rsi_band": ctx.get("rsi_band"),
        "day_of_week": ctx.get("day_of_week"),
        "htf_trend_state": ctx.get("htf_trend_state"),
        "ema_slope_bucket": ctx.get("ema_slope_bucket"),
        "atr_change_bucket": ctx.get("atr_change_bucket"),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def compute_context(
    candles: pd.DataFrame,
    signal_idx: int,
    historical_atr_pcts: list[float] | None = None,
    htf_trend_state: str | None = None,
) -> dict:
    """Compute causal context features at signal_idx using data up to and including that bar.

    Everything here reads `window` — bars at or before the signal — including the
    ATR terciles. Deriving the terciles from the full frame would leak the forward
    bars the labeler needs into the volatility bucket.
    """
    window = candles.iloc[: signal_idx + 1].copy()
    # DuckDB hands back tz-aware timestamps that pandas renders in the machine's
    # local zone. The session bands and day-of-week are UTC definitions, so this
    # has to happen before anything reads an hour or a date off a bar.
    window["ts"] = to_utc_series(window["ts"])
    close = window["close"]
    ema = _ema(close, settings.ema_period)
    atr_series = _atr(window, settings.atr_period)
    rsi_series = _rsi(close, settings.rsi_period)

    signal_close = float(close.iloc[-1])
    ema_val = float(ema.iloc[-1]) if not pd.isna(ema.iloc[-1]) else signal_close
    atr_val = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 0.0
    rsi_val = float(rsi_series.iloc[-1]) if not pd.isna(rsi_series.iloc[-1]) else 50.0

    trend_state = "up" if signal_close > ema_val else "down"
    atr_pct = atr_val / signal_close if signal_close else 0.0

    if historical_atr_pcts:
        pcts = list(historical_atr_pcts)
    else:
        pcts = (atr_series / close).dropna().tolist()
    terciles = (
        float(np.percentile(pcts, 33)) if pcts else 0.0,
        float(np.percentile(pcts, 67)) if pcts else 0.0,
    )

    ts = window["ts"].iloc[-1]
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()

    warmup_available = len(window)
    day_high, day_low = _day_high_low(window, ts)
    dist_day_high_atr = (day_high - signal_close) / atr_val if atr_val else None
    dist_day_low_atr = (signal_close - day_low) / atr_val if atr_val else None

    lookback = settings.ema_slope_lookback
    ema_slope_atr = None
    if len(ema) > lookback and atr_val:
        past_ema = float(ema.iloc[-1 - lookback]) if not pd.isna(ema.iloc[-1 - lookback]) else ema_val
        ema_slope_atr = (ema_val - past_ema) / atr_val

    atr_change_ratio = None
    atr_lb = settings.atr_change_lookback
    if len(atr_series) > atr_lb and not pd.isna(atr_series.iloc[-1 - atr_lb]):
        past_atr = float(atr_series.iloc[-1 - atr_lb])
        atr_change_ratio = atr_val / past_atr if past_atr else None

    signal_bar = candles.iloc[signal_idx]
    gap_atr = None
    if signal_idx > 0 and atr_val:
        prev_close = float(candles.iloc[signal_idx - 1]["close"])
        gap_atr = (float(signal_bar["open"]) - prev_close) / atr_val

    anatomy = _signal_candle_anatomy(signal_bar, atr_val)
    swing = _bars_since_swing(window, SWING_LOOKBACK)

    return {
        "trend_state": trend_state,
        "atr_bucket": atr_bucket(atr_pct, terciles),
        "session": session_from_ts(ts),
        "rsi_band": rsi_band(rsi_val),
        "day_of_week": day_of_week_from_ts(ts),
        "atr_at_signal": atr_val,
        "ema_slope_bucket": ema_slope_bucket(ema_slope_atr),
        "atr_change_bucket": atr_change_bucket(atr_change_ratio),
        "htf_trend_state": htf_trend_state,
        "dist_day_high_atr": dist_day_high_atr,
        "dist_day_low_atr": dist_day_low_atr,
        "gap_atr": gap_atr,
        **anatomy,
        **swing,
        # Raw values alongside the buckets so thresholds can be re-cut later
        # without re-labelling anything.
        "ema_value": ema_val,
        "rsi_value": rsi_val,
        "atr_pct": atr_pct,
        "dist_ema_atr": (signal_close - ema_val) / atr_val if atr_val else None,
        "ema_slope_atr": ema_slope_atr,
        "atr_change_ratio": atr_change_ratio,
        "atr_terciles": list(terciles),
        "warmup_bars_available": warmup_available,
        "context_reliable": warmup_available >= settings.ema_period,
    }
