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
        try:
            stochrsi = ta.stochrsi(df['close'], length=rsi_period, rsi_length=rsi_period, k=k_smooth, d=d_smooth)
            if stochrsi is not None and len(stochrsi.columns) > 0:
                # Try to find the correct column names (pandas-ta may use different naming)
                k_col = next((c for c in stochrsi.columns if 'STOCHRSIk' in str(c) or 'STOCH_RSI_k' in str(c) or 'k' in str(c).lower()), None)
                d_col = next((c for c in stochrsi.columns if 'STOCHRSId' in str(c) or 'STOCH_RSI_d' in str(c) or 'd' in str(c).lower()), None)
                
                if k_col and d_col:
                    result['stochrsi_k'] = stochrsi[k_col]
                    result['stochrsi_d'] = stochrsi[d_col]
                elif len(stochrsi.columns) >= 2:
                    # Fallback: use first two columns if naming doesn't match
                    result['stochrsi_k'] = stochrsi.iloc[:, 0]
                    result['stochrsi_d'] = stochrsi.iloc[:, 1]
                else:
                    raise ValueError("pandas-ta stochrsi returned unexpected format")
            else:
                raise ValueError("pandas-ta stochrsi returned None or empty")
        except Exception as e:
            # If pandas-ta fails, use fallback implementation
            import warnings
            warnings.warn(f"pandas-ta stochrsi failed: {e}, using fallback implementation")
            # Fall through to fallback implementation below
    
    if not PANDAS_TA_AVAILABLE or 'stochrsi_k' not in result.columns:
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
