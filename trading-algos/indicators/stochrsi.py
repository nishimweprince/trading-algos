import pandas_ta as ta
import pandas as pd

def calculate_stochrsi(df: pd.DataFrame, length: int = 14, rsi_length: int = 14, k: int = 3, d: int = 3) -> pd.DataFrame:
    """
    Calculates Stochastic RSI using pandas-ta.
    
    Args:
        df: DataFrame with 'close' column.
        length: Stochastic length.
        rsi_length: RSI length.
        k: K period.
        d: D period.
        
    Returns:
        DataFrame with 'k' and 'd' columns.
    """
    # Ensure column names are lowercase
    df.columns = [c.lower() for c in df.columns]
    
    stochrsi = ta.stochrsi(df['close'], length=length, rsi_length=rsi_length, k=k, d=d)
    
    # pandas-ta returns columns like STOCHRSIk_14_14_3_3, STOCHRSId_14_14_3_3
    col_suffix = f"_{length}_{rsi_length}_{k}_{d}"
    
    result = pd.DataFrame(index=df.index)
    result['k'] = stochrsi[f'STOCHRSIk{col_suffix}']
    result['d'] = stochrsi[f'STOCHRSId{col_suffix}']
    
    return result

def detect_momentum_from_oversold(k_series: pd.Series, oversold_threshold: float = 30, confirm_threshold: float = 60) -> pd.Series:
    """
    Detects when StochRSI K moves from oversold zone.
    
    Args:
        k_series: Series of StochRSI K values.
        oversold_threshold: Threshold for oversold (e.g., 30).
        confirm_threshold: Threshold to confirm valid bounce (e.g., 60).
        
    Returns:
        Boolean Series indicating the condition.
    """
    # Was oversold in previous candle
    was_oversold = k_series.shift(1) < oversold_threshold
    # Currently crossed above threshold
    crossed_above = k_series >= oversold_threshold
    # Not yet overbought/too high
    not_too_high = k_series < confirm_threshold
    
    return was_oversold & crossed_above & not_too_high
