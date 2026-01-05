"""Supertrend Indicator using pandas-ta"""
import pandas as pd
import numpy as np
from loguru import logger

try:
    import pandas_ta as ta
    PANDAS_TA_AVAILABLE = True
except ImportError:
    PANDAS_TA_AVAILABLE = False

def calculate_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0, source: str = 'hl2') -> pd.DataFrame:
    """Calculate Supertrend indicator. Returns df with st_trend, st_value, st_signal columns."""
    result = df.copy()

    if PANDAS_TA_AVAILABLE:
        supertrend = ta.supertrend(df['high'], df['low'], df['close'], length=period, multiplier=multiplier)
        result['st_trend'] = supertrend[f'SUPERTd_{period}_{multiplier}']
        result['st_value'] = supertrend[f'SUPERT_{period}_{multiplier}']
        result['st_lower'] = supertrend[f'SUPERTl_{period}_{multiplier}']
        result['st_upper'] = supertrend[f'SUPERTs_{period}_{multiplier}']
    else:
        # Fallback implementation
        src = (df['high'] + df['low']) / 2
        tr = pd.concat([df['high'] - df['low'], abs(df['high'] - df['close'].shift(1)), abs(df['low'] - df['close'].shift(1))], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean()
        basic_upper, basic_lower = src + (multiplier * atr), src - (multiplier * atr)

        n = len(df)
        final_upper, final_lower, trend = np.zeros(n), np.zeros(n), np.zeros(n)
        final_upper[0], final_lower[0], trend[0] = basic_upper.iloc[0], basic_lower.iloc[0], 1
        close = df['close'].values

        for i in range(1, n):
            final_upper[i] = basic_upper.iloc[i] if basic_upper.iloc[i] < final_upper[i-1] or close[i-1] > final_upper[i-1] else final_upper[i-1]
            final_lower[i] = basic_lower.iloc[i] if basic_lower.iloc[i] > final_lower[i-1] or close[i-1] < final_lower[i-1] else final_lower[i-1]
            trend[i] = 1 if (trend[i-1] == -1 and close[i] > final_lower[i]) else (-1 if (trend[i-1] == 1 and close[i] < final_upper[i]) else trend[i-1])

        result['st_trend'], result['st_value'] = trend, np.where(trend == 1, final_lower, final_upper)
        result['st_upper'], result['st_lower'] = final_upper, final_lower

    result['st_signal'] = 0
    trend_change = result['st_trend'].diff()
    result.loc[trend_change == 2, 'st_signal'] = 1
    result.loc[trend_change == -2, 'st_signal'] = -1
    return result
