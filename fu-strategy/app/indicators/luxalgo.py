"""LuxAlgo Reversal Signal indicator.

Port of the Relative Strength Index (RSI) crossover/crossunder reversal signals
found inside 'LuxAlgo® - Signals & Overlays™'.
"""
from typing import List, Optional
import pandas as pd
from app.core.types import Direction


def rma(series: pd.Series, length: int) -> pd.Series:
    """TradingView Running Moving Average (RMA).

    Equivalent to exponential moving average with alpha = 1 / length.
    """
    alpha = 1.0 / length
    return series.ewm(alpha=alpha, adjust=False).mean()


def calculate_rsi(close_series: pd.Series, length: int = 14) -> pd.Series:
    """Calculate Relative Strength Index (RSI) using TV's exact RMA logic."""
    change = close_series.diff()
    up = change.clip(lower=0)
    down = -change.clip(upper=0)
    
    rma_up = rma(up, length)
    rma_dn = rma(down, length)
    
    # Avoid division by zero
    rs = rma_up / rma_dn
    rsi = 100.0 - (100.0 / (1.0 + rs))
    
    # Mirror Pine's logic for boundary conditions
    rsi = rsi.where(rma_dn != 0, 100.0)
    rsi = rsi.where(rma_up != 0, 0.0)
    
    return rsi


class LuxAlgoReversalEvent:
    def __init__(self, time: pd.Timestamp, direction: Direction, price: float,
                 rsi_value: float, open: Optional[float] = None,
                 high: Optional[float] = None, low: Optional[float] = None,
                 close: Optional[float] = None):
        self.time = time
        self.direction = direction
        self.price = price
        self.rsi_value = rsi_value
        self.open = float(price if open is None else open)
        self.high = float(price if high is None else high)
        self.low = float(price if low is None else low)
        self.close = float(price if close is None else close)


def detect_reversals(df: pd.DataFrame,
                      sensitivity: int = 14,
                      overbought: float = 75.0,
                      oversold: float = 25.0) -> List[LuxAlgoReversalEvent]:
    """Scan a DataFrame and return all LuxAlgo Reversal signals (oldest first)."""
    if len(df) < 2:
        return []

    close_series = df['close']
    rsi = calculate_rsi(close_series, sensitivity)
    
    events: List[LuxAlgoReversalEvent] = []
    
    for i in range(1, len(df)):
        prev_rsi = rsi.iloc[i - 1]
        curr_rsi = rsi.iloc[i]
        
        if pd.isna(prev_rsi) or pd.isna(curr_rsi):
            continue
            
        # Reversal Up: crossover(source, oversold)
        is_up = prev_rsi <= oversold and curr_rsi > oversold
        
        # Reversal Down: crossunder(source, overbought)
        is_down = prev_rsi >= overbought and curr_rsi < overbought
        
        # Use pandas index time
        ts = df.index[i]
        
        if is_up:
            events.append(LuxAlgoReversalEvent(
                time=ts,
                direction=Direction.BUY,
                price=float(df['close'].iloc[i]),
                rsi_value=float(curr_rsi),
                open=float(df['open'].iloc[i]),
                high=float(df['high'].iloc[i]),
                low=float(df['low'].iloc[i]),
                close=float(df['close'].iloc[i]),
            ))
        elif is_down:
            events.append(LuxAlgoReversalEvent(
                time=ts,
                direction=Direction.SELL,
                price=float(df['close'].iloc[i]),
                rsi_value=float(curr_rsi),
                open=float(df['open'].iloc[i]),
                high=float(df['high'].iloc[i]),
                low=float(df['low'].iloc[i]),
                close=float(df['close'].iloc[i]),
            ))
            
    return events


def detect_latest_reversal(df: pd.DataFrame, **kwargs) -> Optional[LuxAlgoReversalEvent]:
    """Return only the reversal event for the most recent closed candle, if any."""
    if len(df) < 2:
        return None
    events = detect_reversals(df, **kwargs)
    if not events:
        return None
    
    latest_time = df.index[-1]
    latest = events[-1]
    return latest if latest.time == latest_time else None
