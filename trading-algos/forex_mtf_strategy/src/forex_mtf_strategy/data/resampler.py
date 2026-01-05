from __future__ import annotations

import pandas as pd


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """
    Resample an OHLCV DataFrame to a higher timeframe.

    Assumes df index is datetime-like.
    """
    ohlcv = df.resample(rule).agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    return ohlcv.dropna()


def forward_fill_to_base(
    base_index: pd.DatetimeIndex, higher_series: pd.Series
) -> pd.Series:
    """
    Align a higher-timeframe series to a base timeframe by forward-filling.
    """
    return higher_series.reindex(base_index, method="ffill")

