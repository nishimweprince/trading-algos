import numpy as np
from typing import NamedTuple
import jesse.indicators as ta


class StochRSIResult(NamedTuple):
    """Stochastic RSI indicator result"""
    k: float  # %K line (0-100)
    d: float  # %D line (signal line, 0-100)
    is_oversold: bool  # Below oversold threshold
    is_overbought: bool  # Above overbought threshold
    crossed_above_oversold: bool  # Just crossed above oversold level
    crossed_below_overbought: bool  # Just crossed below overbought level


def stochrsi(candles: np.ndarray, rsi_period: int = 14, stoch_period: int = 14,
             k_smooth: int = 3, d_smooth: int = 3, oversold: float = 20.0,
             overbought: float = 80.0) -> StochRSIResult:
    """
    Calculate Stochastic RSI indicator.

    StochRSI = (RSI - Lowest RSI) / (Highest RSI - Lowest RSI) * 100

    This applies the Stochastic formula to RSI values instead of price,
    providing a more sensitive momentum indicator that oscillates between 0-100.

    Args:
        candles: Jesse candle array [timestamp, open, close, high, low, volume]
        rsi_period: Period for RSI calculation (default 14)
        stoch_period: Period for Stochastic calculation on RSI (default 14)
        k_smooth: Smoothing period for %K line (default 3)
        d_smooth: Smoothing period for %D line (default 3)
        oversold: Oversold threshold (default 20)
        overbought: Overbought threshold (default 80)

    Returns:
        StochRSIResult with K, D values and signal conditions
    """
    min_candles = rsi_period + stoch_period + max(k_smooth, d_smooth) + 1

    if len(candles) < min_candles:
        return StochRSIResult(
            k=50.0,
            d=50.0,
            is_oversold=False,
            is_overbought=False,
            crossed_above_oversold=False,
            crossed_below_overbought=False
        )

    # Get RSI values
    rsi_values = ta.rsi(candles, period=rsi_period, sequential=True)

    # Calculate Stochastic on RSI
    stoch_k = np.zeros(len(rsi_values))

    for i in range(stoch_period - 1, len(rsi_values)):
        window = rsi_values[i - stoch_period + 1:i + 1]
        lowest = np.min(window)
        highest = np.max(window)

        if highest - lowest == 0:
            stoch_k[i] = 50.0
        else:
            stoch_k[i] = ((rsi_values[i] - lowest) / (highest - lowest)) * 100

    # Apply smoothing to %K
    if k_smooth > 1:
        smoothed_k = _sma(stoch_k, k_smooth)
    else:
        smoothed_k = stoch_k

    # Calculate %D (SMA of smoothed %K)
    stoch_d = _sma(smoothed_k, d_smooth)

    # Current values
    current_k = float(smoothed_k[-1])
    current_d = float(stoch_d[-1])
    prev_k = float(smoothed_k[-2]) if len(smoothed_k) > 1 else current_k

    # Conditions
    is_oversold = current_k < oversold
    is_overbought = current_k > overbought
    crossed_above_oversold = prev_k <= oversold and current_k > oversold
    crossed_below_overbought = prev_k >= overbought and current_k < overbought

    return StochRSIResult(
        k=current_k,
        d=current_d,
        is_oversold=is_oversold,
        is_overbought=is_overbought,
        crossed_above_oversold=crossed_above_oversold,
        crossed_below_overbought=crossed_below_overbought
    )


def stochrsi_sequential(candles: np.ndarray, rsi_period: int = 14, stoch_period: int = 14,
                        k_smooth: int = 3, d_smooth: int = 3) -> dict:
    """
    Calculate Stochastic RSI returning full arrays for all candles.

    Args:
        candles: Jesse candle array
        rsi_period: Period for RSI calculation
        stoch_period: Period for Stochastic calculation
        k_smooth: Smoothing for %K
        d_smooth: Smoothing for %D

    Returns:
        Dictionary with 'k' and 'd' arrays (0-100 scale)
    """
    min_candles = rsi_period + stoch_period + max(k_smooth, d_smooth) + 1

    if len(candles) < min_candles:
        return {
            'k': np.full(len(candles), 50.0),
            'd': np.full(len(candles), 50.0)
        }

    # Get RSI values
    rsi_values = ta.rsi(candles, period=rsi_period, sequential=True)

    # Calculate raw Stochastic on RSI
    stoch_k = np.zeros(len(rsi_values))

    for i in range(stoch_period - 1, len(rsi_values)):
        window = rsi_values[i - stoch_period + 1:i + 1]
        lowest = np.min(window)
        highest = np.max(window)

        if highest - lowest == 0:
            stoch_k[i] = 50.0
        else:
            stoch_k[i] = ((rsi_values[i] - lowest) / (highest - lowest)) * 100

    # Apply smoothing
    if k_smooth > 1:
        smoothed_k = _sma_sequential(stoch_k, k_smooth)
    else:
        smoothed_k = stoch_k

    stoch_d = _sma_sequential(smoothed_k, d_smooth)

    return {
        'k': smoothed_k,
        'd': stoch_d
    }


def _sma(arr: np.ndarray, period: int) -> np.ndarray:
    """Simple Moving Average"""
    result = np.zeros(len(arr))
    for i in range(len(arr)):
        if i < period - 1:
            result[i] = np.mean(arr[:i + 1]) if i > 0 else arr[i]
        else:
            result[i] = np.mean(arr[i - period + 1:i + 1])
    return result


def _sma_sequential(arr: np.ndarray, period: int) -> np.ndarray:
    """Simple Moving Average for sequential calculation"""
    return _sma(arr, period)
