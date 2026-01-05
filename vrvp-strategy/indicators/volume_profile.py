"""Volume Profile Indicator"""
import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import List

@dataclass
class VolumeProfileResult:
    poc: float
    vah: float
    val: float
    hvn: List[float]
    lvn: List[float]

def calculate_volume_profile(df: pd.DataFrame, num_bins: int = 50, value_area_pct: float = 0.70) -> pd.DataFrame:
    """Calculate Volume Profile. Returns df with VP columns."""
    result = df.copy()
    if len(df) < 10:
        result['vp_poc'], result['vp_vah'], result['vp_val'] = df['close'], df['high'], df['low']
        result['vp_near_poc'], result['vp_near_vah'], result['vp_near_val'], result['vp_in_lvn'] = False, False, False, False
        return result

    vp = _calculate_profile(df, num_bins, value_area_pct)
    result['vp_poc'], result['vp_vah'], result['vp_val'] = vp.poc, vp.vah, vp.val

    tr = pd.concat([df['high'] - df['low'], abs(df['high'] - df['close'].shift(1)), abs(df['low'] - df['close'].shift(1))], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]

    result['vp_near_poc'] = abs(df['close'] - vp.poc) <= atr
    result['vp_near_vah'] = abs(df['close'] - vp.vah) <= atr
    result['vp_near_val'] = abs(df['close'] - vp.val) <= atr
    result['vp_in_lvn'] = pd.Series(False, index=df.index)
    for lvn in vp.lvn:
        result['vp_in_lvn'] |= abs(df['close'] - lvn) <= atr * 0.5
    return result

def _calculate_profile(df: pd.DataFrame, num_bins: int, value_area_pct: float) -> VolumeProfileResult:
    high, low, volume = df['high'].values, df['low'].values, df['volume'].values
    price_min, price_max = low.min(), high.max()

    if price_min == price_max:
        return VolumeProfileResult(poc=price_min, vah=price_min, val=price_min, hvn=[price_min], lvn=[])

    bin_size = (price_max - price_min) / num_bins
    price_levels = np.linspace(price_min + bin_size/2, price_max - bin_size/2, num_bins)
    volume_at_price = np.zeros(num_bins)

    for i in range(len(df)):
        low_bin = max(0, min(int((low[i] - price_min) / bin_size), num_bins - 1))
        high_bin = max(0, min(int((high[i] - price_min) / bin_size), num_bins - 1))
        vol_per_bin = volume[i] / (high_bin - low_bin + 1)
        for b in range(low_bin, high_bin + 1):
            volume_at_price[b] += vol_per_bin

    poc_idx = np.argmax(volume_at_price)
    poc = price_levels[poc_idx]

    total_vol, target_vol = volume_at_price.sum(), volume_at_price.sum() * value_area_pct
    va_vol, low_idx, high_idx = volume_at_price[poc_idx], poc_idx, poc_idx

    while va_vol < target_vol and (low_idx > 0 or high_idx < num_bins - 1):
        low_vol = volume_at_price[low_idx - 1] if low_idx > 0 else 0
        high_vol = volume_at_price[high_idx + 1] if high_idx < num_bins - 1 else 0
        if low_vol >= high_vol and low_idx > 0:
            low_idx -= 1
            va_vol += volume_at_price[low_idx]
        elif high_idx < num_bins - 1:
            high_idx += 1
            va_vol += volume_at_price[high_idx]
        else:
            break

    hvn = price_levels[volume_at_price >= np.percentile(volume_at_price, 75)].tolist()
    lvn = price_levels[volume_at_price <= np.percentile(volume_at_price, 25)].tolist()
    return VolumeProfileResult(poc=poc, vah=price_levels[high_idx], val=price_levels[low_idx], hvn=hvn, lvn=lvn)
