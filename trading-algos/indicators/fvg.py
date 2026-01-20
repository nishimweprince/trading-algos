import pandas as pd
import numpy as np

def calculate_fvg(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calculates Fair Value Gaps (FVGs) manually.
    
    A Bullish FVG is formed when:
    - Candle 1 High < Candle 3 Low
    - The gap is (Candle 1 High, Candle 3 Low)
    
    A Bearish FVG is formed when:
    - Candle 1 Low > Candle 3 High
    - The gap is (Candle 3 High, Candle 1 Low)
    
    Args:
        df: DataFrame with 'high', 'low' columns.
        
    Returns:
        DataFrame with columns 'top', 'bottom', 'type' (1 for bullish, -1 for bearish, 0 for none).
        The index corresponds to the 3rd candle (the one that completes the FVG pattern).
    """
    # Ensure column names are lowercase
    df.columns = [c.lower() for c in df.columns]
    
    high = df['high']
    low = df['low']
    
    # Initialize result DataFrame
    fvg = pd.DataFrame(index=df.index, columns=['top', 'bottom', 'type'])
    fvg['type'] = 0
    fvg['top'] = np.nan
    fvg['bottom'] = np.nan
    
    # We need to look back 2 candles (current is i, prev is i-1, prev-prev is i-2)
    # Using shift to align Candle 1 (i-2) and Candle 3 (i)
    
    # Bullish FVG: Low of candle i > High of candle i-2
    # Gap is between High[i-2] and Low[i]
    candle1_high = high.shift(2)
    candle3_low = low
    
    bullish_fvg_mask = candle3_low > candle1_high
    
    # Bearish FVG: High of candle i < Low of candle i-2
    # Gap is between High[i] and Low[i-2]
    # Wait, strictly: Bearish FVG is Low[i-2] > High[i]
    candle1_low = low.shift(2)
    candle3_high = high
    
    bearish_fvg_mask = candle1_low > candle3_high
    
    # Fill Data
    # For Bullish
    fvg.loc[bullish_fvg_mask, 'type'] = 1
    fvg.loc[bullish_fvg_mask, 'bottom'] = candle1_high[bullish_fvg_mask]
    fvg.loc[bullish_fvg_mask, 'top'] = candle3_low[bullish_fvg_mask]
    
    # For Bearish
    fvg.loc[bearish_fvg_mask, 'type'] = -1
    fvg.loc[bearish_fvg_mask, 'top'] = candle1_low[bearish_fvg_mask]
    fvg.loc[bearish_fvg_mask, 'bottom'] = candle3_high[bearish_fvg_mask]
    
    return fvg

def detect_fvg_bounce(row, active_fvgs):
    """
    Checks if the current price action is bouncing off a bullish FVG.
    
    Args:
        row: Current candle data (Series).
        active_fvgs: List of dicts with keys 'top', 'bottom', 'type'.
    
    Returns:
        True if bouncing off a bullish FVG.
    """
    if not active_fvgs:
        return False
        
    for fvg in active_fvgs:
        if fvg['type'] == 1: # Bullish
            # Price entered the zone (Low <= Top and Low >= Bottom)
            entered_zone = row['low'] <= fvg['top'] and row['low'] >= fvg['bottom']
            # Price closed above zone (Close > Top) - simple bounce confirmation
            bounced = row['close'] > fvg['top']
            
            if entered_zone and bounced:
                return True
    return False
