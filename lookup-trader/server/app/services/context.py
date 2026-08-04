from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from app.config import settings


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


def compute_context(
    candles: pd.DataFrame,
    signal_idx: int,
    historical_atr_pcts: list[float] | None = None,
) -> dict:
    """Compute causal context features at signal_idx using data up to and including that bar.

    Everything here reads `window` — bars at or before the signal — including the
    ATR terciles. Deriving the terciles from the full frame would leak the forward
    bars the labeler needs into the volatility bucket.
    """
    window = candles.iloc[: signal_idx + 1].copy()
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

    ts = candles.iloc[signal_idx]["ts"]
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()

    warmup_available = len(window)
    return {
        "trend_state": trend_state,
        "atr_bucket": atr_bucket(atr_pct, terciles),
        "session": session_from_ts(ts),
        "rsi_band": rsi_band(rsi_val),
        "atr_at_signal": atr_val,
        # Raw values alongside the buckets so thresholds can be re-cut later
        # without re-labelling anything.
        "ema_value": ema_val,
        "rsi_value": rsi_val,
        "atr_pct": atr_pct,
        "dist_ema_atr": (signal_close - ema_val) / atr_val if atr_val else None,
        "atr_terciles": list(terciles),
        "warmup_bars_available": warmup_available,
        "context_reliable": warmup_available >= settings.ema_period,
    }
