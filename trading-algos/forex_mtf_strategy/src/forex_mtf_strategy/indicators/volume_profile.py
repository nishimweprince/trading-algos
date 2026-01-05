from __future__ import annotations

import numpy as np
import pandas as pd


def pip_size_for_symbol(symbol: str | None, fallback: float = 0.0001) -> float:
    """
    Best-effort pip size inference.
    - JPY pairs: 0.01
    - Otherwise: 0.0001
    """
    if not symbol:
        return fallback
    s = symbol.upper().replace("_", "").replace("/", "")
    if "JPY" in s:
        return 0.01
    return 0.0001


def volume_profile_context(
    df: pd.DataFrame,
    *,
    window_bars: int = 240,
    num_bins: int = 48,
    hvn_sigma: float = 1.0,
    near_level_pips: float = 5.0,
    pip_size: float = 0.0001,
) -> pd.Series:
    """
    Rolling volume profile context:
    - builds a price histogram (weighted by volume) over a rolling window
    - computes POC (highest volume bin) and HVN bins (volume > mean + sigma*std)
    - flags True when close is near POC or any HVN (within near_level_pips)
    """
    closes = df["close"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    vols = df["volume"].to_numpy(dtype=float)

    near = np.zeros(len(df), dtype=bool)
    near_dist = near_level_pips * pip_size

    # Use typical price (HLC3) for a stable histogram.
    typical = (highs + lows + closes) / 3.0

    for i in range(len(df)):
        start = max(0, i - window_bars + 1)
        if i - start + 1 < max(50, num_bins * 2):
            continue

        window_prices = typical[start : i + 1]
        window_vols = vols[start : i + 1]

        pmin = float(np.min(window_prices))
        pmax = float(np.max(window_prices))
        if not np.isfinite(pmin) or not np.isfinite(pmax) or pmin == pmax:
            continue

        hist, edges = np.histogram(
            window_prices, bins=num_bins, range=(pmin, pmax), weights=window_vols
        )
        if hist.sum() <= 0:
            continue

        mids = (edges[:-1] + edges[1:]) / 2.0
        poc_idx = int(np.argmax(hist))
        poc = float(mids[poc_idx])

        mean = float(np.mean(hist))
        std = float(np.std(hist))
        threshold = mean + hvn_sigma * std
        hvn_mids = mids[hist >= threshold] if std > 0 else np.array([poc])

        price = closes[i]
        if abs(price - poc) <= near_dist:
            near[i] = True
            continue
        if hvn_mids.size and np.min(np.abs(hvn_mids - price)) <= near_dist:
            near[i] = True

    return pd.Series(near, index=df.index, name="in_volume_zone")

