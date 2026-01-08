from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from smartmoneyconcepts import smc


@dataclass(frozen=True)
class FVGZone:
    direction: int  # 1 bullish, -1 bearish
    low: float
    high: float
    created_pos: int
    mitigated_pos: int | None


def compute_fvg(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute Fair Value Gaps using smartmoneyconcepts.

    Returns a DataFrame with columns: FVG, Top, Bottom, MitigatedIndex
    """
    ohlc = df[["open", "high", "low", "close"]].copy()
    ohlc.columns = ["open", "high", "low", "close"]
    out = smc.fvg(ohlc)
    if out is None or len(out) == 0:
        raise ValueError("smartmoneyconcepts.smc.fvg returned empty result")
    return out


def detect_fvg_bounces(df: pd.DataFrame, fvg_df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """
    Detect "bounces" off active FVG zones.

    Bullish bounce:
      - low enters [zone_low, zone_high]
      - close finishes above zone_high

    Bearish bounce:
      - high enters [zone_low, zone_high]
      - close finishes below zone_low
    """
    bullish = np.zeros(len(df), dtype=bool)
    bearish = np.zeros(len(df), dtype=bool)

    active: list[FVGZone] = []

    fvg_vals = fvg_df.get("FVG")
    top_vals = fvg_df.get("Top")
    bot_vals = fvg_df.get("Bottom")
    mit_vals = fvg_df.get("MitigatedIndex")
    if fvg_vals is None or top_vals is None or bot_vals is None:
        raise ValueError(f"Unexpected FVG dataframe columns: {list(fvg_df.columns)}")

    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)

    for i in range(len(df)):
        # Drop mitigated zones
        if active:
            active = [
                z for z in active if (z.mitigated_pos is None) or (i < z.mitigated_pos)
            ]

        fvg_type = fvg_vals.iloc[i]
        if pd.notna(fvg_type) and int(fvg_type) in (1, -1):
            top = float(top_vals.iloc[i])
            bot = float(bot_vals.iloc[i])
            zone_low = min(top, bot)
            zone_high = max(top, bot)
            mit = mit_vals.iloc[i] if mit_vals is not None else np.nan
            mitigated_pos = int(mit) if pd.notna(mit) else None
            active.append(
                FVGZone(
                    direction=int(fvg_type),
                    low=zone_low,
                    high=zone_high,
                    created_pos=i,
                    mitigated_pos=mitigated_pos,
                )
            )

        if not active:
            continue

        lo = lows[i]
        hi = highs[i]
        cl = closes[i]

        for z in active:
            if z.direction == 1:
                entered = (lo <= z.high) and (lo >= z.low)
                bounced = cl > z.high
                if entered and bounced:
                    bullish[i] = True
                    break
            else:
                entered = (hi >= z.low) and (hi <= z.high)
                bounced = cl < z.low
                if entered and bounced:
                    bearish[i] = True
                    break

    return (
        pd.Series(bullish, index=df.index, name="fvg_bullish_bounce"),
        pd.Series(bearish, index=df.index, name="fvg_bearish_bounce"),
    )

