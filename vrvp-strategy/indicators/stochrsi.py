"""Stochastic RSI Indicator using pandas-ta"""
import pandas as pd
import numpy as np

try:
    import pandas_ta as ta
    PANDAS_TA_AVAILABLE = True
except ImportError:
    PANDAS_TA_AVAILABLE = False

def calculate_stochrsi(df: pd.DataFrame, rsi_period: int = 14, stoch_period: int = 14, k_smooth: int = 3,
                       d_smooth: int = 3, oversold: float = 20.0, overbought: float = 80.0) -> pd.DataFrame:
    """Calculate Stochastic RSI. Returns df with stochrsi_k, stochrsi_d, and signal columns."""
    result = df.copy()

    if PANDAS_TA_AVAILABLE:
        stochrsi = ta.stochrsi(df['close'], length=rsi_period, rsi_length=rsi_period, k=k_smooth, d=d_smooth)
        result['stochrsi_k'] = stochrsi[f'STOCHRSIk_{rsi_period}_{rsi_period}_{k_smooth}_{d_smooth}']
        result['stochrsi_d'] = stochrsi[f'STOCHRSId_{rsi_period}_{rsi_period}_{k_smooth}_{d_smooth}']
    else:
        delta = df['close'].diff()
        gain, loss = delta.where(delta > 0, 0.0), -delta.where(delta < 0, 0.0)
        avg_gain, avg_loss = gain.ewm(span=rsi_period, adjust=False).mean(), loss.ewm(span=rsi_period, adjust=False).mean()
        rsi = 100 - (100 / (1 + avg_gain / avg_loss))
        rsi_min, rsi_max = rsi.rolling(stoch_period).min(), rsi.rolling(stoch_period).max()
        stoch_rsi = ((rsi - rsi_min) / (rsi_max - rsi_min) * 100).fillna(50)
        result['stochrsi_k'] = stoch_rsi.rolling(k_smooth).mean()
        result['stochrsi_d'] = result['stochrsi_k'].rolling(d_smooth).mean()

    result['stochrsi_oversold'] = result['stochrsi_k'] < oversold
    result['stochrsi_overbought'] = result['stochrsi_k'] > overbought
    prev_k = result['stochrsi_k'].shift(1)
    result['stochrsi_cross_up'] = (prev_k <= oversold) & (result['stochrsi_k'] > oversold)
    result['stochrsi_cross_down'] = (prev_k >= overbought) & (result['stochrsi_k'] < overbought)
    return result
