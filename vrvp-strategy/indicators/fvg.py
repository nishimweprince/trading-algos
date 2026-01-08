"""Fair Value Gap (FVG) Detection using smartmoneyconcepts"""
import pandas as pd
import numpy as np
from loguru import logger
from typing import Optional

try:
    from smartmoneyconcepts import smc
    SMC_AVAILABLE = True
except ImportError:
    SMC_AVAILABLE = False

def detect_fvg(
    df: pd.DataFrame, 
    max_zones: int = 20, 
    threshold_pct: float = 0.0,
    auto_threshold: bool = False,
    min_gap_pct: Optional[float] = None  # Deprecated, for backward compatibility
) -> pd.DataFrame:
    """
    Detect Fair Value Gaps. Returns df with FVG columns.
    
    Args:
        df: DataFrame with OHLC data
        max_zones: Maximum number of FVG zones to track
        threshold_pct: Manual threshold percentage (0-100). If 0 and auto_threshold=False, uses default 0.01%
        auto_threshold: Enable auto threshold calculation (cumulative average of price range %)
        min_gap_pct: Deprecated - kept for backward compatibility. Use threshold_pct instead.
    
    Returns:
        DataFrame with FVG columns: fvg_signal, fvg_top, fvg_bottom, bullish_fvg, bearish_fvg,
        in_bullish_fvg, in_bearish_fvg, bounce_bullish_fvg, bounce_bearish_fvg
    """
    result = df.copy()
    
    # Handle deprecated parameter for backward compatibility
    if min_gap_pct is not None:
        logger.warning("min_gap_pct parameter is deprecated. Use threshold_pct instead.")
        if threshold_pct == 0.0:
            threshold_pct = min_gap_pct * 100  # Convert to percentage

    if SMC_AVAILABLE:
        try:
            smc_df = df[['open', 'high', 'low', 'close']].copy()
            smc_df.columns = smc_df.columns.str.lower()
            fvg_data = smc.fvg(smc_df)
            result['fvg_signal'] = fvg_data['FVG']
            result['fvg_top'] = fvg_data['Top']
            result['fvg_bottom'] = fvg_data['Bottom']
            result['bullish_fvg'] = result['fvg_signal'] == 1
            result['bearish_fvg'] = result['fvg_signal'] == -1
        except Exception as e:
            logger.warning(f"smartmoneyconcepts FVG failed: {e}")
            result = _detect_fvg_fallback(result, threshold_pct, auto_threshold)
    else:
        result = _detect_fvg_fallback(result, threshold_pct, auto_threshold)

    result = _detect_fvg_interaction(result, max_zones)
    return result

def _calculate_threshold(df: pd.DataFrame, threshold_pct: float, auto_threshold: bool) -> float:
    """
    Calculate threshold for FVG detection.
    
    Args:
        df: DataFrame with OHLC data
        threshold_pct: Manual threshold percentage (0-100)
        auto_threshold: If True, calculate auto threshold based on cumulative average
    
    Returns:
        Threshold value as decimal (e.g., 0.0001 for 0.01%)
    """
    if auto_threshold:
        # Auto threshold: cumulative average of (high - low) / low
        price_range_pct = (df['high'] - df['low']) / df['low']
        n = len(price_range_pct)
        if n > 0:
            cum_sum = price_range_pct.cumsum()
            # Calculate cumulative average: sum / count
            cum_avg = cum_sum / np.arange(1, n + 1)
            return float(cum_avg.iloc[-1])
        return 0.0001  # Default fallback
    
    # Manual threshold: convert percentage to decimal
    if threshold_pct == 0.0:
        return 0.0001  # Default 0.01%
    return threshold_pct / 100.0


def _detect_fvg_fallback(df: pd.DataFrame, threshold_pct: float, auto_threshold: bool) -> pd.DataFrame:
    """
    Fallback FVG detection with improved logic matching Pine Script.
    
    Detection rules (matching Pine Script):
    - Bullish FVG: low[i] > high[i-2] AND close[i-1] > high[i-2] AND (low[i] - high[i-2]) / high[i-2] > threshold
    - Bearish FVG: high[i] < low[i-2] AND close[i-1] < low[i-2] AND (low[i-2] - high[i]) / high[i] > threshold
    """
    result = df.copy()
    n = len(df)
    bullish_fvg, bearish_fvg = np.zeros(n, dtype=bool), np.zeros(n, dtype=bool)
    fvg_top, fvg_bottom = np.zeros(n, dtype=float), np.zeros(n, dtype=float)
    high, low, close = df['high'].values, df['low'].values, df['close'].values
    
    # Calculate threshold
    threshold = _calculate_threshold(df, threshold_pct, auto_threshold)
    
    for i in range(2, n):
        # Bullish FVG: requires gap AND close[1] confirmation
        if low[i] > high[i-2] and close[i-1] > high[i-2]:
            gap_pct = (low[i] - high[i-2]) / high[i-2]
            if gap_pct > threshold:
                bullish_fvg[i] = True
                fvg_top[i] = low[i]
                fvg_bottom[i] = high[i-2]
        
        # Bearish FVG: requires gap AND close[1] confirmation
        elif high[i] < low[i-2] and close[i-1] < low[i-2]:
            gap_pct = (low[i-2] - high[i]) / high[i]
            if gap_pct > threshold:
                bearish_fvg[i] = True
                fvg_top[i] = low[i-2]
                fvg_bottom[i] = high[i]

    result['bullish_fvg'] = bullish_fvg
    result['bearish_fvg'] = bearish_fvg
    result['fvg_top'] = fvg_top
    result['fvg_bottom'] = fvg_bottom
    result['fvg_signal'] = np.where(bullish_fvg, 1, np.where(bearish_fvg, -1, 0))
    return result

def _validate_fvg_zone(top: float, bottom: float) -> bool:
    """Validate that FVG zone has valid boundaries (top > bottom and both are non-zero)."""
    if pd.isna(top) or pd.isna(bottom):
        return False
    if top == 0.0 or bottom == 0.0:
        return False
    return top > bottom


def _track_fvg_mitigation(
    active_zones: list, 
    close: float, 
    is_bullish: bool
) -> list:
    """
    Track FVG mitigation and remove mitigated zones from active list.
    
    Args:
        active_zones: List of (top, bottom, index) tuples
        close: Current close price
        is_bullish: True for bullish FVGs, False for bearish
    
    Returns:
        Updated list with mitigated zones removed
    """
    unmitigated = []
    for zone_top, zone_bottom, zone_idx in active_zones:
        if not _validate_fvg_zone(zone_top, zone_bottom):
            continue
        
        # Check if zone is mitigated
        if is_bullish:
            # Bullish FVG mitigated when price closes below bottom
            if close < zone_bottom:
                continue  # Skip this mitigated zone
        else:
            # Bearish FVG mitigated when price closes above top
            if close > zone_top:
                continue  # Skip this mitigated zone
        
        unmitigated.append((zone_top, zone_bottom, zone_idx))
    
    return unmitigated


def _detect_fvg_interaction(df: pd.DataFrame, max_zones: int = 20) -> pd.DataFrame:
    """
    Detect FVG interactions (price entering zones, bounces) with mitigation tracking.
    Only considers unmitigated FVGs.
    """
    result = df.copy()
    n = len(df)
    in_bullish, in_bearish = np.zeros(n, dtype=bool), np.zeros(n, dtype=bool)
    bounce_bullish, bounce_bearish = np.zeros(n, dtype=bool), np.zeros(n, dtype=bool)
    high, low, close = df['high'].values, df['low'].values, df['close'].values

    active_bullish, active_bearish = [], []
    
    for i in range(len(df)):
        # Add new FVGs to active lists
        if result['bullish_fvg'].iloc[i]:
            zone_top = result['fvg_top'].iloc[i]
            zone_bottom = result['fvg_bottom'].iloc[i]
            if _validate_fvg_zone(zone_top, zone_bottom):
                active_bullish.append((zone_top, zone_bottom, i))
                active_bullish = active_bullish[-max_zones:]
        
        if result['bearish_fvg'].iloc[i]:
            zone_top = result['fvg_top'].iloc[i]
            zone_bottom = result['fvg_bottom'].iloc[i]
            if _validate_fvg_zone(zone_top, zone_bottom):
                active_bearish.append((zone_top, zone_bottom, i))
                active_bearish = active_bearish[-max_zones:]
        
        # Track mitigation - remove mitigated zones
        active_bullish = _track_fvg_mitigation(active_bullish, close[i], is_bullish=True)
        active_bearish = _track_fvg_mitigation(active_bearish, close[i], is_bullish=False)
        
        # Check for price interaction with bullish FVGs
        for zone_top, zone_bottom, _ in active_bullish:
            if not _validate_fvg_zone(zone_top, zone_bottom):
                continue
            
            # Price is in bullish FVG zone
            if low[i] <= zone_top and low[i] >= zone_bottom:
                in_bullish[i] = True
                # Bounce: price closes above zone top
                if close[i] > zone_top:
                    bounce_bullish[i] = True
                break
        
        # Check for price interaction with bearish FVGs
        for zone_top, zone_bottom, _ in active_bearish:
            if not _validate_fvg_zone(zone_top, zone_bottom):
                continue
            
            # Price is in bearish FVG zone
            if high[i] >= zone_bottom and high[i] <= zone_top:
                in_bearish[i] = True
                # Bounce: price closes below zone bottom
                if close[i] < zone_bottom:
                    bounce_bearish[i] = True
                break

    result['in_bullish_fvg'] = in_bullish
    result['in_bearish_fvg'] = in_bearish
    result['bounce_bullish_fvg'] = bounce_bullish
    result['bounce_bearish_fvg'] = bounce_bearish
    return result
