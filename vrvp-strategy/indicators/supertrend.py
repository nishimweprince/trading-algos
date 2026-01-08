"""
Supertrend Indicator Implementation

This module provides a Supertrend indicator implementation that aligns with standard
implementations (PineScript, freqtrade) while maintaining backward compatibility.

Direction Encoding:
    - 1 = Uptrend (price above supertrend line)
    - -1 = Downtrend (price below supertrend line)
    
Note: This is opposite to PineScript convention (-1=uptrend, 1=downtrend) but matches
the existing codebase expectations in signal_generator.py.

ATR Calculation:
    Uses Simple Moving Average (SMA) for ATR calculation, matching standard implementations.

First Bar Handling:
    First 'period' bars are set to NaN since ATR requires 'period' bars of historical data.
    Calculations start from index 'period'.

Source Parameter:
    Supports multiple source options:
    - 'hl2': (high + low) / 2 (default)
    - 'close': close price
    - 'open': open price
    - 'high': high price
    - 'low': low price
    - 'hlc3': (high + low + close) / 3
    - 'ohlc4': (open + high + low + close) / 4
"""
import pandas as pd
import numpy as np
from loguru import logger

try:
    import pandas_ta as ta
    PANDAS_TA_AVAILABLE = True
except ImportError:
    PANDAS_TA_AVAILABLE = False


def _get_source(df: pd.DataFrame, source: str) -> pd.Series:
    """
    Calculate source series based on source parameter.
    
    Args:
        df: DataFrame with OHLC data
        source: Source type ('hl2', 'close', 'open', 'high', 'low', 'hlc3', 'ohlc4')
    
    Returns:
        Series with calculated source values
    """
    source_map = {
        'hl2': (df['high'] + df['low']) / 2,
        'close': df['close'],
        'open': df['open'],
        'high': df['high'],
        'low': df['low'],
        'hlc3': (df['high'] + df['low'] + df['close']) / 3,
        'ohlc4': (df['open'] + df['high'] + df['low'] + df['close']) / 4,
    }
    return source_map.get(source.lower(), source_map['hl2'])


def calculate_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0, source: str = 'hl2') -> pd.DataFrame:
    """
    Calculate Supertrend indicator.
    
    This implementation follows standard Supertrend algorithm:
    1. Calculate True Range (TR)
    2. Calculate ATR using Simple Moving Average (SMA)
    3. Calculate basic upper and lower bands
    4. Calculate final upper and lower bands (with trailing logic)
    5. Determine trend direction based on price position relative to bands
    
    Args:
        df: DataFrame with OHLC data (must contain 'high', 'low', 'close' columns)
        period: ATR period (default: 10)
        multiplier: ATR multiplier for band calculation (default: 3.0)
        source: Source price for calculation ('hl2', 'close', 'open', 'high', 'low', 'hlc3', 'ohlc4')
    
    Returns:
        DataFrame with added columns:
        - st_trend: Trend direction (1=uptrend, -1=downtrend)
        - st_value: Current Supertrend line value
        - st_upper: Upper band value
        - st_lower: Lower band value
        - st_signal: Signal on trend change (1=uptrend change, -1=downtrend change, 0=no change)
    """
    result = df.copy()

    # Try pandas-ta implementation first
    if PANDAS_TA_AVAILABLE:
        try:
            supertrend = ta.supertrend(df['high'], df['low'], df['close'], length=period, multiplier=multiplier)
            if supertrend is not None and len(supertrend.columns) > 0:
                # Try to find columns with expected naming
                trend_col = next((c for c in supertrend.columns if 'SUPERTd' in str(c) or 'trend' in str(c).lower()), None)
                value_col = next((c for c in supertrend.columns if 'SUPERT_' in str(c) and 'SUPERTd' not in str(c) and 'SUPERTl' not in str(c) and 'SUPERTs' not in str(c)), None)
                lower_col = next((c for c in supertrend.columns if 'SUPERTl' in str(c)), None)
                upper_col = next((c for c in supertrend.columns if 'SUPERTs' in str(c)), None)
                
                if trend_col and value_col:
                    result['st_trend'] = supertrend[trend_col]
                    result['st_value'] = supertrend[value_col] if value_col else supertrend.iloc[:, 1]
                    result['st_lower'] = supertrend[lower_col] if lower_col else supertrend.iloc[:, 2] if len(supertrend.columns) > 2 else result['st_value']
                    result['st_upper'] = supertrend[upper_col] if upper_col else supertrend.iloc[:, 3] if len(supertrend.columns) > 3 else result['st_value']
                else:
                    raise ValueError("pandas-ta supertrend returned unexpected format")
            else:
                raise ValueError("pandas-ta supertrend returned None or empty")
        except Exception as e:
            logger.warning(f"pandas-ta supertrend failed: {e}, using fallback implementation")
            # Fall through to fallback implementation below
    
    # Fallback implementation (standard Supertrend algorithm)
    if not PANDAS_TA_AVAILABLE or 'st_trend' not in result.columns:
        # Calculate source price based on source parameter
        src = _get_source(df, source)
        
        # Calculate True Range (TR)
        tr = pd.concat([
            df['high'] - df['low'],
            abs(df['high'] - df['close'].shift(1)),
            abs(df['low'] - df['close'].shift(1))
        ], axis=1).max(axis=1)
        
        # Calculate ATR using Simple Moving Average (SMA) - standard implementation
        atr = tr.rolling(period).mean()
        
        # Calculate basic upper and lower bands
        basic_upper = src + (multiplier * atr)
        basic_lower = src - (multiplier * atr)

        n = len(df)
        # Initialize arrays with NaN for first 'period' bars (ATR requires period bars of data)
        final_upper = np.full(n, np.nan)
        final_lower = np.full(n, np.nan)
        trend = np.full(n, np.nan)
        
        close = df['close'].values

        # Start calculations from 'period' index (standard implementation pattern)
        # Initialize first valid bar
        if n > period:
            final_upper[period] = basic_upper.iloc[period]
            final_lower[period] = basic_lower.iloc[period]
            # Determine initial trend based on close position relative to bands
            # If close is above basic_upper, we're in uptrend (ST = final_lower)
            # If close is below basic_lower, we're in downtrend (ST = final_upper)
            # Otherwise, default to downtrend (conservative approach)
            if close[period] > basic_upper.iloc[period]:
                trend[period] = 1  # Uptrend
            elif close[period] < basic_lower.iloc[period]:
                trend[period] = -1  # Downtrend
            else:
                # Close is between bands - determine based on which band is closer
                # or default to downtrend (matches standard behavior)
                trend[period] = -1  # Default to downtrend

        # Calculate final bands and trend for remaining bars
        for i in range(period + 1, n):
            # Final upper band: use basic_upper if it's lower than previous final_upper
            # or if previous close crossed above previous final_upper
            if basic_upper.iloc[i] < final_upper[i-1] or close[i-1] > final_upper[i-1]:
                final_upper[i] = basic_upper.iloc[i]
            else:
                final_upper[i] = final_upper[i-1]
            
            # Final lower band: use basic_lower if it's higher than previous final_lower
            # or if previous close crossed below previous final_lower
            if basic_lower.iloc[i] > final_lower[i-1] or close[i-1] < final_lower[i-1]:
                final_lower[i] = basic_lower.iloc[i]
            else:
                final_lower[i] = final_lower[i-1]
            
            # Determine trend direction (standard Supertrend algorithm)
            # The trend switches when:
            # - From downtrend to uptrend: close crosses above final_lower
            # - From uptrend to downtrend: close crosses below final_upper
            if trend[i-1] == -1:
                # Currently in downtrend
                if close[i] > final_lower[i]:
                    # Close crossed above final_lower -> switch to uptrend
                    trend[i] = 1
                else:
                    # Continue downtrend
                    trend[i] = -1
            else:
                # Currently in uptrend (trend[i-1] == 1)
                if close[i] < final_upper[i]:
                    # Close crossed below final_upper -> switch to downtrend
                    trend[i] = -1
                else:
                    # Continue uptrend
                    trend[i] = 1

        # Set Supertrend value: use final_lower for uptrend, final_upper for downtrend
        # This matches the standard algorithm where ST line follows the active band
        st_value = np.where(trend == 1, final_lower, final_upper)
        
        result['st_trend'] = trend
        result['st_value'] = st_value
        result['st_upper'] = final_upper
        result['st_lower'] = final_lower

    # Generate signals on trend changes
    result['st_signal'] = 0
    trend_change = result['st_trend'].diff()
    # Signal = 1 when trend changes from -1 to 1 (downtrend to uptrend)
    result.loc[trend_change == 2, 'st_signal'] = 1
    # Signal = -1 when trend changes from 1 to -1 (uptrend to downtrend)
    result.loc[trend_change == -2, 'st_signal'] = -1
    
    return result
