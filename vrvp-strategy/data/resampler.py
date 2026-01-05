"""Multi-Timeframe Data Resampling"""
import pandas as pd
from loguru import logger

def resample_to_htf(df: pd.DataFrame, target_timeframe: str, shift: bool = True) -> pd.DataFrame:
    """Resample to HTF. CRITICAL: shift=True prevents look-ahead bias."""
    tf_map = {'1M': '1min', '5M': '5min', '15M': '15min', '30M': '30min', '1H': '1h', '4H': '4h', '1D': '1D'}
    resampled = df.resample(tf_map.get(target_timeframe, target_timeframe)).agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}).dropna()
    if shift:
        resampled = resampled.shift(1)
        logger.debug("HTF data shifted by 1 period")
    return resampled

def align_htf_to_ltf(ltf_df: pd.DataFrame, htf_df: pd.DataFrame, htf_column: str) -> pd.Series:
    """Align HTF data to LTF index using forward-fill."""
    return htf_df[htf_column].reindex(ltf_df.index, method='ffill')
