"""
Timeframe resampling utilities for multi-timeframe analysis.

Handles proper aggregation from lower to higher timeframes with look-ahead bias prevention.
"""

from typing import Optional

import pandas as pd

from monitoring.logger import get_logger

logger = get_logger(__name__)


class TimeframeResampler:
    """Resample OHLCV data between timeframes."""
    
    # Standard forex timeframe mappings
    TIMEFRAME_MAP = {
        "M1": "1min",
        "M5": "5min",
        "M15": "15min",
        "M30": "30min",
        "H1": "1h",
        "H4": "4h",
        "D": "1D",
        "W": "1W",
        "M": "1ME",
    }
    
    def __init__(self, df: pd.DataFrame):
        """
        Initialize resampler with base data.
        
        Args:
            df: DataFrame with DatetimeIndex and OHLCV columns
        """
        self.df = df.copy()
        self._validate_data()
    
    def _validate_data(self):
        """Validate input data has required columns."""
        required = ["open", "high", "low", "close"]
        missing = [col for col in required if col not in self.df.columns]
        if missing:
            raise ValueError(f"Missing required columns: {missing}")
        
        if not isinstance(self.df.index, pd.DatetimeIndex):
            raise ValueError("DataFrame must have DatetimeIndex")
    
    def resample(
        self,
        target_timeframe: str,
        shift: bool = True,
    ) -> pd.DataFrame:
        """
        Resample data to a higher timeframe.
        
        Args:
            target_timeframe: Target timeframe (M5, M15, M30, H1, H4, D, W, M)
            shift: If True, shift data by 1 period to prevent look-ahead bias
            
        Returns:
            Resampled DataFrame
        """
        freq = self.TIMEFRAME_MAP.get(target_timeframe, target_timeframe)
        
        # OHLCV aggregation rules
        agg_dict = {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
        }
        
        if "volume" in self.df.columns:
            agg_dict["volume"] = "sum"
        
        resampled = self.df.resample(freq).agg(agg_dict).dropna()
        
        if shift:
            # Shift to only use completed candles (prevents look-ahead bias)
            resampled = resampled.shift(1)
            resampled = resampled.dropna()
            logger.debug(f"Shifted {target_timeframe} data by 1 period for bias prevention")
        
        logger.info(f"Resampled to {target_timeframe}: {len(resampled)} candles")
        return resampled
    
    def merge_timeframes(
        self,
        higher_tf_data: pd.DataFrame,
        columns: Optional[list[str]] = None,
        suffix: str = "",
    ) -> pd.DataFrame:
        """
        Merge higher timeframe data into the base timeframe using forward-fill.
        
        This properly aligns higher timeframe signals with lower timeframe data
        by forward-filling values until the next higher timeframe candle completes.
        
        Args:
            higher_tf_data: Higher timeframe DataFrame
            columns: Columns to merge (all if None)
            suffix: Suffix to add to merged column names
            
        Returns:
            Base DataFrame with merged higher timeframe columns
        """
        if columns is None:
            columns = higher_tf_data.columns.tolist()
        
        result = self.df.copy()
        
        for col in columns:
            if col in higher_tf_data.columns:
                col_name = f"{col}{suffix}" if suffix else col
                
                # Reindex to lower timeframe and forward-fill
                result[col_name] = higher_tf_data[col].reindex(
                    result.index, 
                    method="ffill"
                )
        
        logger.info(f"Merged {len(columns)} columns from higher timeframe")
        return result
    
    @classmethod
    def create_mtf_dataset(
        cls,
        df_base: pd.DataFrame,
        higher_timeframes: list[str],
        indicators_callback: Optional[callable] = None,
    ) -> dict[str, pd.DataFrame]:
        """
        Create a multi-timeframe dataset from base data.
        
        Args:
            df_base: Base timeframe DataFrame
            higher_timeframes: List of higher timeframes to create
            indicators_callback: Optional function to calculate indicators on each timeframe
            
        Returns:
            Dict mapping timeframe names to DataFrames
        """
        resampler = cls(df_base)
        datasets = {"base": df_base.copy()}
        
        for tf in higher_timeframes:
            datasets[tf] = resampler.resample(tf, shift=True)
            
            if indicators_callback:
                datasets[tf] = indicators_callback(datasets[tf])
        
        return datasets


def align_signals(
    base_df: pd.DataFrame,
    signal_df: pd.DataFrame,
    signal_column: str,
    new_column_name: Optional[str] = None,
) -> pd.DataFrame:
    """
    Align signals from a higher timeframe to a lower timeframe.
    
    Args:
        base_df: Lower timeframe DataFrame
        signal_df: Higher timeframe DataFrame with signals
        signal_column: Name of the signal column
        new_column_name: Name for the aligned column (defaults to signal_column)
        
    Returns:
        Base DataFrame with aligned signal column
    """
    result = base_df.copy()
    col_name = new_column_name or signal_column
    
    result[col_name] = signal_df[signal_column].reindex(
        result.index,
        method="ffill"
    )
    
    return result


def detect_timeframe(df: pd.DataFrame) -> str:
    """
    Detect the timeframe of a DataFrame based on index frequency.
    
    Args:
        df: DataFrame with DatetimeIndex
        
    Returns:
        Detected timeframe string (M1, M5, H1, etc.)
    """
    if len(df) < 2:
        return "unknown"
    
    # Calculate median time delta
    deltas = df.index.to_series().diff().dropna()
    median_delta = deltas.median()
    
    # Map to timeframe
    minutes = median_delta.total_seconds() / 60
    
    if minutes <= 1:
        return "M1"
    elif minutes <= 5:
        return "M5"
    elif minutes <= 15:
        return "M15"
    elif minutes <= 30:
        return "M30"
    elif minutes <= 60:
        return "H1"
    elif minutes <= 240:
        return "H4"
    elif minutes <= 1440:
        return "D"
    elif minutes <= 10080:
        return "W"
    else:
        return "M"
