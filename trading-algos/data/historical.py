import pandas as pd
import numpy as np

def generate_dummy_data(days=100, timeframe='1H'):
    """Generates dummy OHLCV data."""
    dates = pd.date_range(start='2024-01-01', periods=days*24, freq=timeframe)
    np.random.seed(42)
    
    close = np.random.normal(1.10, 0.01, size=len(dates)).cumsum()
    close = close - close.min() + 1.10 # Ensure positive
    
    df = pd.DataFrame(index=dates)
    df['open'] = close + np.random.normal(0, 0.001, size=len(dates))
    df['high'] = df[['open']].max(axis=1) + np.abs(np.random.normal(0, 0.002, size=len(dates)))
    df['low'] = df[['open']].min(axis=1) - np.abs(np.random.normal(0, 0.002, size=len(dates)))
    df['close'] = close
    df['volume'] = np.random.randint(100, 1000, size=len(dates))
    
    return df
