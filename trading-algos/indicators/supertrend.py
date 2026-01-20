import pandas_ta as ta
import pandas as pd

def calculate_supertrend(df: pd.DataFrame, length: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    """
    Calculates Supertrend indicator using pandas-ta.
    
    Args:
        df: DataFrame with 'high', 'low', 'close' columns.
        length: ATR period.
        multiplier: ATR multiplier.
        
    Returns:
        DataFrame with 'SUPERT' (value) and 'SUPERTd' (direction) columns.
    """
    # Ensure column names are lowercase
    df.columns = [c.lower() for c in df.columns]
    
    supertrend = ta.supertrend(df['high'], df['low'], df['close'], length=length, multiplier=multiplier)
    
    # Rename columns to standard names for easier access
    # pandas-ta returns columns like SUPERT_10_3.0, SUPERTd_10_3.0, SUPERTl_10_3.0, SUPERTs_10_3.0
    col_suffix = f"_{length}_{multiplier}"
    
    result = pd.DataFrame(index=df.index)
    result['value'] = supertrend[f'SUPERT{col_suffix}']
    result['direction'] = supertrend[f'SUPERTd{col_suffix}'] # 1 for bullish, -1 for bearish
    
    return result
