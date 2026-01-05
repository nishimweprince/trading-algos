"""
Stochastic RSI indicator implementation.

Combines RSI and Stochastic oscillator for momentum detection.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

import pandas as pd
import pandas_ta as ta

from config.settings import StochRSIParams, get_settings


@dataclass
class StochRSIResult:
    """Container for StochRSI calculation results."""
    
    k: pd.Series  # %K line (fast)
    d: pd.Series  # %D line (slow/signal)
    is_oversold: pd.Series  # True when K is below oversold level
    is_overbought: pd.Series  # True when K is above overbought level
    from_oversold: pd.Series  # True when crossing up from oversold
    from_overbought: pd.Series  # True when crossing down from overbought


class StochRSIIndicator:
    """
    Stochastic RSI indicator for momentum detection.
    
    StochRSI applies the Stochastic formula to RSI values, creating a more
    sensitive oscillator. Values range from 0-100 (when using pandas-ta).
    
    Key signals:
    - Oversold: K below oversold level (default 30)
    - Overbought: K above overbought level (default 70)
    - "From oversold": K was below oversold, now crossing above
    """
    
    def __init__(
        self,
        length: Optional[int] = None,
        rsi_length: Optional[int] = None,
        k: Optional[int] = None,
        d: Optional[int] = None,
        oversold: Optional[float] = None,
        overbought: Optional[float] = None,
    ):
        """
        Initialize StochRSI indicator.
        
        Args:
            length: StochRSI period
            rsi_length: RSI period
            k: %K smoothing period
            d: %D smoothing period
            oversold: Oversold threshold (0-100)
            overbought: Overbought threshold (0-100)
        """
        settings = get_settings()
        params = settings.strategy.stochrsi
        
        self.length = length or params.length
        self.rsi_length = rsi_length or params.rsi_length
        self.k = k or params.k
        self.d = d or params.d
        self.oversold = oversold or params.oversold
        self.overbought = overbought or params.overbought
    
    def calculate(self, df: pd.DataFrame) -> StochRSIResult:
        """
        Calculate StochRSI indicator.
        
        Args:
            df: DataFrame with 'close' column
            
        Returns:
            StochRSIResult with indicator values and signals
        """
        # Use pandas-ta for calculation
        result = ta.stochrsi(
            close=df["close"],
            length=self.length,
            rsi_length=self.rsi_length,
            k=self.k,
            d=self.d,
        )
        
        # Column names from pandas-ta
        k_col = f"STOCHRSIk_{self.length}_{self.rsi_length}_{self.k}_{self.d}"
        d_col = f"STOCHRSId_{self.length}_{self.rsi_length}_{self.k}_{self.d}"
        
        k_line = result[k_col]
        d_line = result[d_col]
        
        # Calculate zone conditions
        is_oversold = k_line < self.oversold
        is_overbought = k_line > self.overbought
        
        # Crossing signals (using shift to check previous bar)
        from_oversold = (
            (k_line.shift(1) < self.oversold) &  # Was oversold
            (k_line >= self.oversold) &           # Crossed above
            (k_line < 60)                          # Not above 60 (still has room)
        )
        
        from_overbought = (
            (k_line.shift(1) > self.overbought) &  # Was overbought
            (k_line <= self.overbought) &           # Crossed below
            (k_line > 40)                            # Not below 40
        )
        
        return StochRSIResult(
            k=k_line,
            d=d_line,
            is_oversold=is_oversold,
            is_overbought=is_overbought,
            from_oversold=from_oversold,
            from_overbought=from_overbought,
        )
    
    def add_to_dataframe(
        self,
        df: pd.DataFrame,
        prefix: str = "stochrsi",
    ) -> pd.DataFrame:
        """
        Add StochRSI columns to DataFrame.
        
        Args:
            df: Input DataFrame
            prefix: Column name prefix
            
        Returns:
            DataFrame with added StochRSI columns
        """
        result = self.calculate(df)
        
        df_out = df.copy()
        df_out[f"{prefix}_k"] = result.k
        df_out[f"{prefix}_d"] = result.d
        df_out[f"{prefix}_oversold"] = result.is_oversold
        df_out[f"{prefix}_overbought"] = result.is_overbought
        df_out[f"{prefix}_from_oversold"] = result.from_oversold
        df_out[f"{prefix}_from_overbought"] = result.from_overbought
        
        return df_out
    
    def get_momentum_state(self, df: pd.DataFrame) -> str:
        """
        Get current momentum state.
        
        Args:
            df: DataFrame with close data
            
        Returns:
            One of: 'oversold', 'overbought', 'from_oversold', 
                    'from_overbought', 'neutral'
        """
        result = self.calculate(df)
        
        if result.from_oversold.iloc[-1]:
            return "from_oversold"
        elif result.from_overbought.iloc[-1]:
            return "from_overbought"
        elif result.is_oversold.iloc[-1]:
            return "oversold"
        elif result.is_overbought.iloc[-1]:
            return "overbought"
        else:
            return "neutral"
    
    def get_divergence(
        self,
        df: pd.DataFrame,
        lookback: int = 20,
    ) -> Optional[str]:
        """
        Detect price/StochRSI divergence.
        
        Args:
            df: DataFrame with close data
            lookback: Number of bars to check for divergence
            
        Returns:
            'bullish', 'bearish', or None
        """
        if len(df) < lookback:
            return None
        
        result = self.calculate(df)
        recent_df = df.iloc[-lookback:]
        recent_k = result.k.iloc[-lookback:]
        
        # Find price and indicator highs/lows
        price_high_idx = recent_df["close"].idxmax()
        price_low_idx = recent_df["close"].idxmin()
        k_high_idx = recent_k.idxmax()
        k_low_idx = recent_k.idxmin()
        
        current_price = df["close"].iloc[-1]
        current_k = result.k.iloc[-1]
        
        # Bullish divergence: price makes lower low, indicator makes higher low
        if (current_price < recent_df.loc[price_low_idx, "close"] and
            current_k > recent_k.min()):
            return "bullish"
        
        # Bearish divergence: price makes higher high, indicator makes lower high
        if (current_price > recent_df.loc[price_high_idx, "close"] and
            current_k < recent_k.max()):
            return "bearish"
        
        return None


def calculate_stochrsi(
    df: pd.DataFrame,
    length: int = 14,
    rsi_length: int = 14,
    k: int = 3,
    d: int = 3,
) -> Tuple[pd.Series, pd.Series]:
    """
    Convenience function to calculate StochRSI.
    
    Args:
        df: DataFrame with close data
        length: StochRSI period
        rsi_length: RSI period
        k: %K smoothing
        d: %D smoothing
        
    Returns:
        Tuple of (%K, %D) Series
    """
    indicator = StochRSIIndicator(
        length=length,
        rsi_length=rsi_length,
        k=k,
        d=d,
    )
    result = indicator.calculate(df)
    return result.k, result.d
