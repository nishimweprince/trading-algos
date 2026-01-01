import numpy as np
from typing import NamedTuple
from jesse.indicators import atr as jesse_atr
from jesse.indicators import sma


class SupertrendResult(NamedTuple):
    """Supertrend indicator result"""
    trend: int  # 1 for uptrend, -1 for downtrend
    upper_band: float  # Upper trend band
    lower_band: float  # Lower trend band
    signal: int  # 1 for buy signal, -1 for sell signal, 0 for no signal


def supertrend(candles: np.ndarray, period: int = 10, multiplier: float = 3.0,
               source: str = 'hl2', use_ema_atr: bool = True) -> SupertrendResult:
    """
    Calculate Supertrend indicator - a trend-following indicator that uses ATR.

    The Supertrend provides dynamic support/resistance levels that adapt to volatility.
    - Uptrend: price above the indicator (green)
    - Downtrend: price below the indicator (red)

    Args:
        candles: Jesse candle array [timestamp, open, close, high, low, volume]
        period: ATR period (default 10)
        multiplier: ATR multiplier for band width (default 3.0)
        source: Price source - 'hl2', 'hlc3', 'ohlc4', 'close' (default 'hl2')
        use_ema_atr: Use EMA-based ATR (True) or SMA-based (False)

    Returns:
        SupertrendResult with trend, bands, and signal
    """
    if len(candles) < period + 1:
        return SupertrendResult(
            trend=0,
            upper_band=0.0,
            lower_band=0.0,
            signal=0
        )

    # Extract OHLC
    opens = candles[:, 1]
    closes = candles[:, 2]
    highs = candles[:, 3]
    lows = candles[:, 4]

    # Calculate source price
    if source == 'hl2':
        src = (highs + lows) / 2
    elif source == 'hlc3':
        src = (highs + lows + closes) / 3
    elif source == 'ohlc4':
        src = (opens + highs + lows + closes) / 4
    elif source == 'close':
        src = closes
    else:
        src = (highs + lows) / 2  # default to hl2

    # Calculate ATR
    if use_ema_atr:
        # Use Jesse's built-in ATR (EMA-based)
        atr_values = jesse_atr(candles, period=period, sequential=True)
    else:
        # Use SMA of True Range
        tr = _true_range(candles)
        atr_values = np.zeros(len(candles))
        for i in range(len(candles)):
            if i < period - 1:
                atr_values[i] = 0
            else:
                atr_values[i] = np.mean(tr[i - period + 1:i + 1])

    # Initialize arrays
    n = len(candles)
    basic_upper_band = src + (multiplier * atr_values)
    basic_lower_band = src - (multiplier * atr_values)

    final_upper_band = np.zeros(n)
    final_lower_band = np.zeros(n)
    supertrend = np.zeros(n)
    trend = np.zeros(n, dtype=int)

    # Initialize first values
    final_upper_band[0] = basic_upper_band[0]
    final_lower_band[0] = basic_lower_band[0]
    trend[0] = 1
    supertrend[0] = final_lower_band[0]

    # Calculate Supertrend
    for i in range(1, n):
        # Upper band: if basic upper < previous final upper OR close[i-1] > previous final upper
        # then use basic upper, else use previous final upper
        if basic_upper_band[i] < final_upper_band[i-1] or closes[i-1] > final_upper_band[i-1]:
            final_upper_band[i] = basic_upper_band[i]
        else:
            final_upper_band[i] = final_upper_band[i-1]

        # Lower band: if basic lower > previous final lower OR close[i-1] < previous final lower
        # then use basic lower, else use previous final lower
        if basic_lower_band[i] > final_lower_band[i-1] or closes[i-1] < final_lower_band[i-1]:
            final_lower_band[i] = basic_lower_band[i]
        else:
            final_lower_band[i] = final_lower_band[i-1]

        # Determine trend
        if trend[i-1] == -1 and closes[i] > final_lower_band[i]:
            trend[i] = 1  # Switch to uptrend
        elif trend[i-1] == 1 and closes[i] < final_upper_band[i]:
            trend[i] = -1  # Switch to downtrend
        else:
            trend[i] = trend[i-1]  # Maintain trend

        # Set supertrend line
        if trend[i] == 1:
            supertrend[i] = final_lower_band[i]
        else:
            supertrend[i] = final_upper_band[i]

    # Detect signal (trend change)
    current_trend = int(trend[-1])
    previous_trend = int(trend[-2]) if len(trend) > 1 else current_trend

    if current_trend == 1 and previous_trend == -1:
        signal = 1  # Buy signal
    elif current_trend == -1 and previous_trend == 1:
        signal = -1  # Sell signal
    else:
        signal = 0  # No signal

    return SupertrendResult(
        trend=current_trend,
        upper_band=float(final_upper_band[-1]),
        lower_band=float(final_lower_band[-1]),
        signal=signal
    )


def supertrend_sequential(candles: np.ndarray, period: int = 10, multiplier: float = 3.0,
                          source: str = 'hl2', use_ema_atr: bool = True) -> dict:
    """
    Calculate Supertrend indicator returning full arrays for all candles.

    Args:
        candles: Jesse candle array
        period: ATR period
        multiplier: ATR multiplier
        source: Price source
        use_ema_atr: Use EMA-based ATR

    Returns:
        Dictionary with 'trend', 'upper_band', 'lower_band', 'supertrend' arrays
    """
    if len(candles) < period + 1:
        return {
            'trend': np.zeros(len(candles), dtype=int),
            'upper_band': np.zeros(len(candles)),
            'lower_band': np.zeros(len(candles)),
            'supertrend': np.zeros(len(candles)),
        }

    # Extract OHLC
    opens = candles[:, 1]
    closes = candles[:, 2]
    highs = candles[:, 3]
    lows = candles[:, 4]

    # Calculate source price
    if source == 'hl2':
        src = (highs + lows) / 2
    elif source == 'hlc3':
        src = (highs + lows + closes) / 3
    elif source == 'ohlc4':
        src = (opens + highs + lows + closes) / 4
    elif source == 'close':
        src = closes
    else:
        src = (highs + lows) / 2

    # Calculate ATR
    if use_ema_atr:
        atr_values = jesse_atr(candles, period=period, sequential=True)
    else:
        tr = _true_range(candles)
        atr_values = np.zeros(len(candles))
        for i in range(len(candles)):
            if i < period - 1:
                atr_values[i] = 0
            else:
                atr_values[i] = np.mean(tr[i - period + 1:i + 1])

    # Calculate bands
    n = len(candles)
    basic_upper_band = src + (multiplier * atr_values)
    basic_lower_band = src - (multiplier * atr_values)

    final_upper_band = np.zeros(n)
    final_lower_band = np.zeros(n)
    supertrend = np.zeros(n)
    trend = np.zeros(n, dtype=int)

    final_upper_band[0] = basic_upper_band[0]
    final_lower_band[0] = basic_lower_band[0]
    trend[0] = 1
    supertrend[0] = final_lower_band[0]

    for i in range(1, n):
        if basic_upper_band[i] < final_upper_band[i-1] or closes[i-1] > final_upper_band[i-1]:
            final_upper_band[i] = basic_upper_band[i]
        else:
            final_upper_band[i] = final_upper_band[i-1]

        if basic_lower_band[i] > final_lower_band[i-1] or closes[i-1] < final_lower_band[i-1]:
            final_lower_band[i] = basic_lower_band[i]
        else:
            final_lower_band[i] = final_lower_band[i-1]

        if trend[i-1] == -1 and closes[i] > final_lower_band[i]:
            trend[i] = 1
        elif trend[i-1] == 1 and closes[i] < final_upper_band[i]:
            trend[i] = -1
        else:
            trend[i] = trend[i-1]

        if trend[i] == 1:
            supertrend[i] = final_lower_band[i]
        else:
            supertrend[i] = final_upper_band[i]

    return {
        'trend': trend,
        'upper_band': final_upper_band,
        'lower_band': final_lower_band,
        'supertrend': supertrend,
    }


def _true_range(candles: np.ndarray) -> np.ndarray:
    """
    Calculate True Range for ATR calculation.

    TR = max(high - low, abs(high - prev_close), abs(low - prev_close))
    """
    highs = candles[:, 3]
    lows = candles[:, 4]
    closes = candles[:, 2]

    tr = np.zeros(len(candles))
    tr[0] = highs[0] - lows[0]

    for i in range(1, len(candles)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i-1])
        lc = abs(lows[i] - closes[i-1])
        tr[i] = max(hl, hc, lc)

    return tr
