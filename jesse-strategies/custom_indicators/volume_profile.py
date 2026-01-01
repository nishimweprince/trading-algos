import numpy as np
from typing import NamedTuple

class VolumeProfileResult(NamedTuple):
    poc: float
    vah: float
    val: float
    profile: np.ndarray
    price_levels: np.ndarray
    hvn: np.ndarray
    lvn: np.ndarray

def volume_profile(candles: np.ndarray, num_bins: int = 50,
                   value_area_pct: float = 0.70,
                   lvn_threshold: float = 0.25) -> VolumeProfileResult:
    """
    Calculate Volume Profile with POC, Value Area, HVN, and LVN.
    
    :param candles: Jesse candle array [timestamp, open, close, high, low, volume]
    :param num_bins: Number of price levels for distribution
    :param value_area_pct: Percentage of volume for Value Area (default 70%)
    :param lvn_threshold: Volume threshold for LVN detection (bottom 25%)
    """
    highs = candles[:, 3]
    lows = candles[:, 4]
    volumes = candles[:, 5]
    
    # Handle single candle or empty case to prevent errors
    if len(candles) == 0:
        return VolumeProfileResult(0, 0, 0, np.array([]), np.array([]), np.array([]), np.array([]))
    
    price_min, price_max = np.min(lows), np.max(highs)
    
    # Handle zero range
    if price_min == price_max:
         return VolumeProfileResult(price_min, price_min, price_min, np.array([np.sum(volumes)]), np.array([price_min]), np.array([price_min]), np.array([]))

    bin_size = (price_max - price_min) / num_bins
    price_levels = np.linspace(price_min + bin_size/2, price_max - bin_size/2, num_bins)
    volume_at_price = np.zeros(num_bins)
    
    # Distribute each candle's volume across price bins it touched
    for i in range(len(candles)):
        low_bin = max(0, min(int((lows[i] - price_min) / bin_size), num_bins - 1))
        high_bin = max(0, min(int((highs[i] - price_min) / bin_size), num_bins - 1))
        bins_touched = high_bin - low_bin + 1
        volume_per_bin = volumes[i] / bins_touched
        for b in range(low_bin, high_bin + 1):
            volume_at_price[b] += volume_per_bin
    
    # POC: highest volume price level
    poc_idx = np.argmax(volume_at_price)
    poc = price_levels[poc_idx]
    
    # Value Area: expand from POC until 70% of volume captured
    total_vol = np.sum(volume_at_price)
    target_vol = total_vol * value_area_pct
    va_vol = volume_at_price[poc_idx]
    low_idx, high_idx = poc_idx, poc_idx
    
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
    
    val, vah = price_levels[low_idx], price_levels[high_idx]
    
    # Identify HVN and LVN
    max_vol = np.max(volume_at_price)
    if max_vol > 0:
        hvn_mask = volume_at_price >= max_vol * (1 - lvn_threshold)
        lvn_mask = volume_at_price <= max_vol * lvn_threshold
    else:
        hvn_mask = np.zeros(num_bins, dtype=bool)
        lvn_mask = np.zeros(num_bins, dtype=bool)

    
    return VolumeProfileResult(
        poc=poc, vah=vah, val=val,
        profile=volume_at_price,
        price_levels=price_levels,
        hvn=price_levels[hvn_mask],
        lvn=price_levels[lvn_mask]
    )
