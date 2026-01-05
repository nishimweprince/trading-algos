"""
Supertrend indicator implementation.

Uses pandas-ta for calculation with additional utility methods for trend analysis.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

import pandas as pd
import pandas_ta as ta

from config.settings import SupertrendParams, get_settings


@dataclass
class SupertrendResult:
    """Container for Supertrend calculation results."""
    
    value: pd.Series  # Supertrend line value
    direction: pd.Series  # 1 = bullish, -1 = bearish
    trend_changed: pd.Series  # True when trend changed
    upper_band: Optional[pd.Series] = None
    lower_band: Optional[pd.Series] = None


class SupertrendIndicator:
    """
    Supertrend indicator for trend direction filtering.
    
    The Supertrend uses ATR-based bands to determine trend direction.
    When price is above the Supertrend line, trend is bullish (direction = 1).
    When price is below the Supertrend line, trend is bearish (direction = -1).
    """
    
    def __init__(
        self,
        length: Optional[int] = None,
        multiplier: Optional[float] = None,
    ):
        """
        Initialize Supertrend indicator.
        
        Args:
            length: ATR period (default from settings)
            multiplier: ATR multiplier for bands (default from settings)
        """
        settings = get_settings()
        params = settings.strategy.supertrend
        
        self.length = length or params.length
        self.multiplier = multiplier or params.multiplier
    
    def calculate(self, df: pd.DataFrame) -> SupertrendResult:
        """
        Calculate Supertrend indicator.
        
        Args:
            df: DataFrame with 'high', 'low', 'close' columns
            
        Returns:
            SupertrendResult with indicator values
        """
        # Use pandas-ta for calculation
        result = ta.supertrend(
            high=df["high"],
            low=df["low"],
            close=df["close"],
            length=self.length,
            multiplier=self.multiplier,
        )
        
        # Column names from pandas-ta
        supert_col = f"SUPERT_{self.length}_{self.multiplier}"
        supertd_col = f"SUPERTd_{self.length}_{self.multiplier}"
        supertu_col = f"SUPERTu_{self.length}_{self.multiplier}"  # Upper band
        supertl_col = f"SUPERTl_{self.length}_{self.multiplier}"  # Lower band
        
        direction = result[supertd_col]
        
        # Detect trend changes
        trend_changed = direction != direction.shift(1)
        
        return SupertrendResult(
            value=result[supert_col],
            direction=direction,
            trend_changed=trend_changed,
            upper_band=result.get(supertu_col),
            lower_band=result.get(supertl_col),
        )
    
    def add_to_dataframe(
        self,
        df: pd.DataFrame,
        prefix: str = "st",
    ) -> pd.DataFrame:
        """
        Add Supertrend columns to DataFrame.
        
        Args:
            df: Input DataFrame
            prefix: Column name prefix
            
        Returns:
            DataFrame with added Supertrend columns
        """
        result = self.calculate(df)
        
        df_out = df.copy()
        df_out[f"{prefix}_value"] = result.value
        df_out[f"{prefix}_direction"] = result.direction
        df_out[f"{prefix}_changed"] = result.trend_changed
        
        return df_out
    
    def get_current_trend(self, df: pd.DataFrame) -> int:
        """
        Get the current trend direction.
        
        Args:
            df: DataFrame with OHLC data
            
        Returns:
            1 for bullish, -1 for bearish, 0 if not enough data
        """
        if len(df) < self.length:
            return 0
        
        result = self.calculate(df)
        return int(result.direction.iloc[-1])
    
    def is_trend_aligned(
        self,
        df: pd.DataFrame,
        expected_direction: int,
    ) -> bool:
        """
        Check if current trend matches expected direction.
        
        Args:
            df: DataFrame with OHLC data
            expected_direction: Expected trend (1 for bullish, -1 for bearish)
            
        Returns:
            True if trend matches expected direction
        """
        current = self.get_current_trend(df)
        return current == expected_direction
    
    def get_trend_duration(self, df: pd.DataFrame) -> int:
        """
        Get the number of candles in the current trend.
        
        Args:
            df: DataFrame with OHLC data
            
        Returns:
            Number of candles since last trend change
        """
        result = self.calculate(df)
        direction = result.direction
        
        # Find last trend change
        changes = direction != direction.shift(1)
        if not changes.any():
            return len(df)
        
        last_change_idx = changes[::-1].idxmax()
        return len(df.loc[last_change_idx:]) - 1


def calculate_supertrend(
    df: pd.DataFrame,
    length: int = 10,
    multiplier: float = 3.0,
) -> Tuple[pd.Series, pd.Series]:
    """
    Convenience function to calculate Supertrend.
    
    Args:
        df: DataFrame with OHLC data
        length: ATR period
        multiplier: ATR multiplier
        
    Returns:
        Tuple of (supertrend_value, direction)
    """
    indicator = SupertrendIndicator(length=length, multiplier=multiplier)
    result = indicator.calculate(df)
    return result.value, result.direction
