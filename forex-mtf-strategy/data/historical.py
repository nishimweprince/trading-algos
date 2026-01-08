"""
Historical data loader for backtesting.

Supports loading data from CSV files (Dukascopy, HistData formats) and OANDA API.
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from config.settings import get_settings
from monitoring.logger import get_logger

logger = get_logger(__name__)


class HistoricalDataLoader:
    """Load historical forex data from various sources."""
    
    def __init__(self, data_dir: Optional[Path] = None):
        """
        Initialize the historical data loader.
        
        Args:
            data_dir: Directory containing historical data files
        """
        settings = get_settings()
        self.data_dir = data_dir or settings.data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
    
    def load_csv(
        self,
        filepath: str,
        date_column: str = "time",
        date_format: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        Load data from a generic CSV file.
        
        Args:
            filepath: Path to CSV file
            date_column: Name of the date/time column
            date_format: Optional strftime format for parsing dates
            
        Returns:
            DataFrame with DatetimeIndex and OHLCV columns
        """
        path = Path(filepath)
        if not path.exists():
            path = self.data_dir / filepath
        
        if not path.exists():
            raise FileNotFoundError(f"Data file not found: {filepath}")
        
        df = pd.read_csv(path)
        
        # Normalize column names
        df.columns = df.columns.str.lower().str.strip()
        
        # Parse date column
        if date_format:
            df[date_column] = pd.to_datetime(df[date_column], format=date_format)
        else:
            df[date_column] = pd.to_datetime(df[date_column])
        
        df.set_index(date_column, inplace=True)
        df.index.name = None
        
        # Ensure required columns exist
        required = ["open", "high", "low", "close"]
        missing = [col for col in required if col not in df.columns]
        if missing:
            raise ValueError(f"Missing required columns: {missing}")
        
        # Add volume if missing
        if "volume" not in df.columns:
            df["volume"] = 0
        
        # Sort by index
        df.sort_index(inplace=True)
        
        # Remove timezone if present
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        
        logger.info(f"Loaded {len(df)} rows from {path.name}")
        return df[["open", "high", "low", "close", "volume"]]
    
    def load_dukascopy(self, filepath: str) -> pd.DataFrame:
        """
        Load data from Dukascopy CSV format.
        
        Dukascopy format: Gmt time,Open,High,Low,Close,Volume
        Example: 01.01.2023 00:00:00.000,1.07047,1.07048,1.07041,1.07046,234.67
        
        Args:
            filepath: Path to Dukascopy CSV file
            
        Returns:
            DataFrame with DatetimeIndex and OHLCV columns
        """
        path = Path(filepath)
        if not path.exists():
            path = self.data_dir / filepath
        
        df = pd.read_csv(path)
        df.columns = df.columns.str.lower().str.strip()
        
        # Parse Dukascopy date format
        date_col = "gmt time" if "gmt time" in df.columns else df.columns[0]
        df["time"] = pd.to_datetime(df[date_col], format="%d.%m.%Y %H:%M:%S.%f")
        df.set_index("time", inplace=True)
        df.index.name = None
        
        # Rename columns
        df = df.rename(columns={
            date_col: "time",
        })
        
        # Keep only OHLCV
        df = df[["open", "high", "low", "close", "volume"]]
        df.sort_index(inplace=True)
        
        logger.info(f"Loaded {len(df)} Dukascopy rows from {path.name}")
        return df
    
    def load_histdata(self, filepath: str) -> pd.DataFrame:
        """
        Load data from HistData.com format.
        
        HistData M1 format: YYYYMMDD HHMMSS;OPEN;HIGH;LOW;CLOSE;VOLUME
        
        Args:
            filepath: Path to HistData CSV file
            
        Returns:
            DataFrame with DatetimeIndex and OHLCV columns
        """
        path = Path(filepath)
        if not path.exists():
            path = self.data_dir / filepath
        
        df = pd.read_csv(
            path,
            sep=";",
            header=None,
            names=["datetime", "open", "high", "low", "close", "volume"],
        )
        
        # Parse datetime
        df["time"] = pd.to_datetime(df["datetime"], format="%Y%m%d %H%M%S")
        df.set_index("time", inplace=True)
        df.index.name = None
        df = df.drop(columns=["datetime"])
        df.sort_index(inplace=True)
        
        logger.info(f"Loaded {len(df)} HistData rows from {path.name}")
        return df
    
    def load_metatrader(self, filepath: str) -> pd.DataFrame:
        """
        Load data exported from MetaTrader 4/5.
        
        MT format: Date,Time,Open,High,Low,Close,Volume
        Example: 2023.01.01,00:00,1.07047,1.07048,1.07041,1.07046,234
        
        Args:
            filepath: Path to MetaTrader CSV file
            
        Returns:
            DataFrame with DatetimeIndex and OHLCV columns
        """
        path = Path(filepath)
        if not path.exists():
            path = self.data_dir / filepath
        
        df = pd.read_csv(path)
        df.columns = df.columns.str.lower().str.strip()
        
        # Combine date and time columns
        df["datetime"] = df["date"].astype(str) + " " + df["time"].astype(str)
        df["time"] = pd.to_datetime(df["datetime"], format="%Y.%m.%d %H:%M")
        df.set_index("time", inplace=True)
        df.index.name = None
        
        df = df[["open", "high", "low", "close", "volume"]]
        df.sort_index(inplace=True)
        
        logger.info(f"Loaded {len(df)} MetaTrader rows from {path.name}")
        return df
    
    def generate_sample_data(
        self,
        instrument: str = "EUR_USD",
        start: datetime = datetime(2023, 1, 1),
        end: datetime = datetime(2024, 1, 1),
        granularity: str = "H1",
    ) -> pd.DataFrame:
        """
        Generate sample OHLCV data for testing.
        
        Args:
            instrument: Instrument name (for realistic price ranges)
            start: Start datetime
            end: End datetime
            granularity: Candle granularity
            
        Returns:
            DataFrame with synthetic OHLCV data
        """
        import numpy as np
        
        # Frequency mapping
        freq_map = {
            "M1": "1min",
            "M5": "5min",
            "M15": "15min",
            "M30": "30min",
            "H1": "1h",
            "H4": "4h",
            "D": "1D",
        }
        
        freq = freq_map.get(granularity, "1h")
        index = pd.date_range(start=start, end=end, freq=freq)
        
        # Base price for common pairs
        base_prices = {
            "EUR_USD": 1.08,
            "GBP_USD": 1.26,
            "USD_JPY": 145.0,
            "AUD_USD": 0.65,
            "USD_CHF": 0.88,
        }
        
        base_price = base_prices.get(instrument, 1.0)
        n = len(index)
        
        # Generate random walk
        np.random.seed(42)
        returns = np.random.normal(0, 0.0005, n)  # ~0.05% volatility per candle
        price_path = base_price * np.cumprod(1 + returns)
        
        # Generate OHLC from price path
        df = pd.DataFrame(index=index)
        df["close"] = price_path
        
        # Add noise for high/low
        volatility = 0.0003 * base_price
        df["high"] = df["close"] + np.abs(np.random.normal(0, volatility, n))
        df["low"] = df["close"] - np.abs(np.random.normal(0, volatility, n))
        df["open"] = df["close"].shift(1).fillna(base_price)
        
        # Ensure OHLC relationship
        df["high"] = df[["open", "high", "close"]].max(axis=1)
        df["low"] = df[["open", "low", "close"]].min(axis=1)
        
        # Generate volume
        df["volume"] = np.random.randint(100, 5000, n)
        
        df = df[["open", "high", "low", "close", "volume"]]
        
        logger.info(f"Generated {len(df)} sample candles for {instrument}")
        return df
    
    def save_to_csv(self, df: pd.DataFrame, filename: str) -> Path:
        """
        Save DataFrame to CSV in the data directory.
        
        Args:
            df: DataFrame to save
            filename: Output filename
            
        Returns:
            Path to saved file
        """
        filepath = self.data_dir / filename
        df.to_csv(filepath)
        logger.info(f"Saved {len(df)} rows to {filepath}")
        return filepath
    
    def list_available_files(self) -> list[str]:
        """List available data files in the data directory."""
        files = []
        for ext in ["*.csv", "*.parquet"]:
            files.extend([f.name for f in self.data_dir.glob(ext)])
        return sorted(files)
