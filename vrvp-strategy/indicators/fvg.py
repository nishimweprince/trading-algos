"""Fair Value Gap (FVG) Detection using smartmoneyconcepts"""
import pandas as pd
import numpy as np
from loguru import logger

try:
    from smartmoneyconcepts import smc
    SMC_AVAILABLE = True
except ImportError:
    SMC_AVAILABLE = False

def detect_fvg(df: pd.DataFrame, max_zones: int = 20, min_gap_pct: float = 0.0001) -> pd.DataFrame:
    """Detect Fair Value Gaps. Returns df with FVG columns."""
    result = df.copy()

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
            result = _detect_fvg_fallback(result, min_gap_pct)
    else:
        result = _detect_fvg_fallback(result, min_gap_pct)

    result = _detect_fvg_interaction(result)
    return result

def _detect_fvg_fallback(df: pd.DataFrame, min_gap_pct: float) -> pd.DataFrame:
    result = df.copy()
    n = len(df)
    bullish_fvg, bearish_fvg = np.zeros(n, dtype=bool), np.zeros(n, dtype=bool)
    fvg_top, fvg_bottom = np.zeros(n), np.zeros(n)
    high, low, close = df['high'].values, df['low'].values, df['close'].values

    for i in range(2, n):
        min_gap = close[i] * min_gap_pct
        if low[i] > high[i-2] and low[i] - high[i-2] >= min_gap:
            bullish_fvg[i], fvg_top[i], fvg_bottom[i] = True, low[i], high[i-2]
        elif high[i] < low[i-2] and low[i-2] - high[i] >= min_gap:
            bearish_fvg[i], fvg_top[i], fvg_bottom[i] = True, low[i-2], high[i]

    result['bullish_fvg'], result['bearish_fvg'] = bullish_fvg, bearish_fvg
    result['fvg_top'], result['fvg_bottom'] = fvg_top, fvg_bottom
    result['fvg_signal'] = np.where(bullish_fvg, 1, np.where(bearish_fvg, -1, 0))
    return result

def _detect_fvg_interaction(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    n = len(df)
    in_bullish, in_bearish = np.zeros(n, dtype=bool), np.zeros(n, dtype=bool)
    bounce_bullish, bounce_bearish = np.zeros(n, dtype=bool), np.zeros(n, dtype=bool)
    high, low, close = df['high'].values, df['low'].values, df['close'].values

    active_bullish, active_bearish = [], []
    for i in range(len(df)):
        if result['bullish_fvg'].iloc[i]:
            active_bullish.append((result['fvg_top'].iloc[i], result['fvg_bottom'].iloc[i], i))
            active_bullish = active_bullish[-20:]
        if result['bearish_fvg'].iloc[i]:
            active_bearish.append((result['fvg_top'].iloc[i], result['fvg_bottom'].iloc[i], i))
            active_bearish = active_bearish[-20:]

        for zone_top, zone_bottom, _ in active_bullish:
            if zone_top and low[i] <= zone_top and low[i] >= zone_bottom:
                in_bullish[i] = True
                if close[i] > zone_top: bounce_bullish[i] = True
                break

        for zone_top, zone_bottom, _ in active_bearish:
            if zone_top and high[i] >= zone_bottom and high[i] <= zone_top:
                in_bearish[i] = True
                if close[i] < zone_bottom: bounce_bearish[i] = True
                break

    result['in_bullish_fvg'], result['in_bearish_fvg'] = in_bullish, in_bearish
    result['bounce_bullish_fvg'], result['bounce_bearish_fvg'] = bounce_bullish, bounce_bearish
    return result
