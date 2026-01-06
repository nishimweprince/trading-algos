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

        # Try to detect if file has headers and separator
        with open(filepath, 'r') as f:
            first_line = f.readline().strip()
            # Check if first line looks like a date (starts with digit or date-like pattern)
            # Handle formats like "01.12.2025 00:00:00.000 UTC,1.16011,..." or "2009-12-22 21:00\t1.42505\t..."
            has_header = False
            sep = '\t'  # Default to tab
            
            # Check for comma-separated format
            if ',' in first_line:
                sep = ','
                parts = first_line.split(',')
                # If first part contains date/time and "UTC", it's likely a header or data row
                # Check if it looks like a date
                first_part = parts[0].strip()
                has_header = not (first_part[0].isdigit() if first_part else False)
            # Check for tab-separated format
            elif '\t' in first_line:
                sep = '\t'
                parts = first_line.split('\t')
                first_part = parts[0].strip() if parts else ''
                has_header = not (first_part[0].isdigit() if first_part else False)
        
        # Read CSV with detected separator
        try:
            df = pd.read_csv(filepath, sep=sep, header=0 if has_header else None)
        except Exception as e:
            # Fallback: try auto-detection
            df = pd.read_csv(filepath, header=0 if has_header else None)

        # If no header, assign standard column names
        if not has_header:
            if len(df.columns) >= 5:
                df.columns = ['timestamp', 'open', 'high', 'low', 'close'] + [f'col_{i}' for i in range(5, len(df.columns))]
            else:
                raise ValueError(f"Expected at least 5 columns (timestamp, open, high, low, close), got {len(df.columns)}")
        
        # Convert column names to lowercase strings (handle both string and numeric column names)
        df.columns = [str(col).lower().strip() for col in df.columns]
        
        # Handle timestamp column that might contain "UTC" suffix or be in first column
        # If timestamp column contains "UTC", split it
        timestamp_col = next((c for c in ['timestamp', 'date', 'time', 'datetime'] if c in df.columns), df.columns[0])
        if timestamp_col in df.columns:
            # Check if timestamp contains "UTC" - if so, extract just the date/time part
            if df[timestamp_col].dtype == 'object':
                # Try to extract date part if it contains "UTC"
                df[timestamp_col] = df[timestamp_col].astype(str).str.split(' UTC').str[0].str.strip()

        # Find date column
        date_col = next((c for c in ['timestamp', 'date', 'time', 'datetime'] if c in df.columns), None)
        if not date_col:
            # If no named date column, assume first column is the date
            date_col = df.columns[0]
            logger.info(f"No date column found, using first column '{date_col}' as timestamp")

        # Parse date column
        df[date_col] = pd.to_datetime(df[date_col], format=date_format, errors='coerce') if date_format else pd.to_datetime(df[date_col], errors='coerce')
        
        # Remove rows with invalid dates
        df = df.dropna(subset=[date_col])
        
        df.set_index(date_col, inplace=True)
        df.index.name = 'timestamp'

        # Ensure we have the required columns
        required_cols = ['open', 'high', 'low', 'close']
        missing_cols = [c for c in required_cols if c not in df.columns]
        if missing_cols:
            raise ValueError(f"Missing required columns: {missing_cols}. Found columns: {list(df.columns)}")

        # Handle volume column
        if 'volume' not in df.columns:
            df['volume'] = 0
        
        # Select and convert columns
        df = df[['open', 'high', 'low', 'close', 'volume']].astype({'open': float, 'high': float, 'low': float, 'close': float, 'volume': int})
        df.sort_index(inplace=True)
        df = df[~df.index.duplicated(keep='first')]

        self._cache[cache_key] = df.copy()
        
        # Detect and log the timeframe of the loaded data
        if len(df) > 1:
            time_diff = df.index[1] - df.index[0]
            if time_diff.total_seconds() == 60:
                detected_tf = '1M'
            elif time_diff.total_seconds() == 300:
                detected_tf = '5M'
            elif time_diff.total_seconds() == 900:
                detected_tf = '15M'
            elif time_diff.total_seconds() == 1800:
                detected_tf = '30M'
            elif time_diff.total_seconds() == 3600:
                detected_tf = '1H'
            elif time_diff.total_seconds() == 14400:
                detected_tf = '4H'
            elif time_diff.total_seconds() == 86400:
                detected_tf = '1D'
            else:
                detected_tf = f'{time_diff}'
            logger.info(f"Loaded {len(df)} candles from {filepath} (detected timeframe: {detected_tf})")
        else:
            logger.info(f"Loaded {len(df)} candles from {filepath}")
        
        return df

    def resample(self, df: pd.DataFrame, target_timeframe: str) -> pd.DataFrame:
        tf_map = {'1M': '1T', '5M': '5T', '15M': '15T', '30M': '30T', '1H': '1h', '4H': '4h', '1D': '1D'}
        return df.resample(tf_map.get(target_timeframe, target_timeframe)).agg(
            {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}).dropna()

    def get_date_range(self, df: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
        # Include full day for end date (set to end of day)
        from datetime import timedelta
        end_inclusive = end.replace(hour=23, minute=59, second=59, microsecond=999999)
        return df.loc[(df.index >= start) & (df.index <= end_inclusive)].copy()
