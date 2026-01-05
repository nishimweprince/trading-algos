"""Historical Data Loader"""
import pandas as pd
from pathlib import Path
from datetime import datetime
from typing import Optional
from loguru import logger

class HistoricalDataLoader:
    def __init__(self, data_dir: str = 'data/historical'):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._cache = {}

    def load_csv(self, filepath: str, date_column: str = 'timestamp', date_format: Optional[str] = None) -> pd.DataFrame:
        path = Path(filepath)
        if not path.exists(): raise FileNotFoundError(f"Data file not found: {filepath}")

        cache_key = str(path.absolute())
        if cache_key in self._cache: return self._cache[cache_key].copy()

        df = pd.read_csv(filepath)
        df.columns = df.columns.str.lower().str.strip()

        date_col = next((c for c in ['timestamp', 'date', 'time', 'datetime'] if c in df.columns), None)
        if not date_col: raise ValueError("No date column found")

        df[date_col] = pd.to_datetime(df[date_col], format=date_format) if date_format else pd.to_datetime(df[date_col])
        df.set_index(date_col, inplace=True)
        df.index.name = 'timestamp'

        if 'volume' not in df.columns: df['volume'] = 0
        df = df[['open', 'high', 'low', 'close', 'volume']].astype({'open': float, 'high': float, 'low': float, 'close': float, 'volume': int})
        df.sort_index(inplace=True)
        df = df[~df.index.duplicated(keep='first')]

        self._cache[cache_key] = df.copy()
        logger.info(f"Loaded {len(df)} candles from {filepath}")
        return df

    def resample(self, df: pd.DataFrame, target_timeframe: str) -> pd.DataFrame:
        tf_map = {'1M': '1T', '5M': '5T', '15M': '15T', '30M': '30T', '1H': '1H', '4H': '4H', '1D': '1D'}
        return df.resample(tf_map.get(target_timeframe, target_timeframe)).agg(
            {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}).dropna()

    def get_date_range(self, df: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
        return df.loc[(df.index >= start) & (df.index <= end)].copy()
