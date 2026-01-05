import numpy as np
from typing import NamedTuple, List, Optional
from dataclasses import dataclass


@dataclass
class FVGZone:
    """Represents a Fair Value Gap zone"""
    type: str  # 'bullish' or 'bearish'
    top: float  # Upper boundary of the gap
    bottom: float  # Lower boundary of the gap
    created_index: int  # Candle index when FVG was created
    mitigated: bool  # Whether the gap has been filled
    mitigated_index: Optional[int]  # Index when mitigated (if applicable)


class FVGResult(NamedTuple):
    """Fair Value Gap detection result"""
    bullish_fvg: bool  # Bullish FVG detected on current candle
    bearish_fvg: bool  # Bearish FVG detected on current candle
    fvg_top: float  # Top of the detected FVG (0 if none)
    fvg_bottom: float  # Bottom of the detected FVG (0 if none)
    active_zones: List[FVGZone]  # List of active (unmitigated) FVG zones
    price_in_bullish_fvg: bool  # Current price is in a bullish FVG
    price_in_bearish_fvg: bool  # Current price is in a bearish FVG
    bouncing_off_bullish_fvg: bool  # Price entered and bounced from bullish FVG
    bouncing_off_bearish_fvg: bool  # Price entered and bounced from bearish FVG


def fvg(candles: np.ndarray, max_zones: int = 20,
        min_gap_atr_mult: float = 0.1) -> FVGResult:
    """
    Detect Fair Value Gaps (FVG) - imbalance zones in price action.

    A Fair Value Gap is a 3-candle pattern:
    - Bullish FVG: Candle 3's low > Candle 1's high (gap up in middle candle)
    - Bearish FVG: Candle 3's high < Candle 1's low (gap down in middle candle)

    These gaps often act as support/resistance and tend to get "filled" over time.

    Args:
        candles: Jesse candle array [timestamp, open, close, high, low, volume]
        max_zones: Maximum number of active FVG zones to track (default 20)
        min_gap_atr_mult: Minimum gap size as ATR multiple (default 0.1)

    Returns:
        FVGResult with current FVG detection and active zone tracking
    """
    if len(candles) < 3:
        return FVGResult(
            bullish_fvg=False,
            bearish_fvg=False,
            fvg_top=0.0,
            fvg_bottom=0.0,
            active_zones=[],
            price_in_bullish_fvg=False,
            price_in_bearish_fvg=False,
            bouncing_off_bullish_fvg=False,
            bouncing_off_bearish_fvg=False
        )

    # Extract OHLC
    closes = candles[:, 2]
    highs = candles[:, 3]
    lows = candles[:, 4]

    # Calculate simple ATR for minimum gap filter
    atr = _calculate_atr(candles, period=14)
    min_gap_size = atr * min_gap_atr_mult

    # Track active FVG zones
    active_zones: List[FVGZone] = []

    # Scan for all FVGs and track them
    for i in range(2, len(candles)):
        # Bullish FVG: candle[i] low > candle[i-2] high
        if lows[i] > highs[i - 2]:
            gap_size = lows[i] - highs[i - 2]
            if gap_size >= min_gap_size:
                zone = FVGZone(
                    type='bullish',
                    top=lows[i],
                    bottom=highs[i - 2],
                    created_index=i,
                    mitigated=False,
                    mitigated_index=None
                )
                active_zones.append(zone)

        # Bearish FVG: candle[i] high < candle[i-2] low
        elif highs[i] < lows[i - 2]:
            gap_size = lows[i - 2] - highs[i]
            if gap_size >= min_gap_size:
                zone = FVGZone(
                    type='bearish',
                    top=lows[i - 2],
                    bottom=highs[i],
                    created_index=i,
                    mitigated=False,
                    mitigated_index=None
                )
                active_zones.append(zone)

    # Check mitigation of zones
    current_idx = len(candles) - 1
    current_low = lows[-1]
    current_high = highs[-1]
    current_close = closes[-1]
    prev_low = lows[-2] if len(candles) > 1 else current_low
    prev_high = highs[-2] if len(candles) > 1 else current_high

    for zone in active_zones:
        if zone.mitigated:
            continue

        # Check if zone was mitigated
        if zone.type == 'bullish':
            # Bullish FVG mitigated when price falls into the gap
            if current_low <= zone.top:
                zone.mitigated = True
                zone.mitigated_index = current_idx
        else:
            # Bearish FVG mitigated when price rises into the gap
            if current_high >= zone.bottom:
                zone.mitigated = True
                zone.mitigated_index = current_idx

    # Keep only unmitigated zones (limited to max_zones most recent)
    active_zones = [z for z in active_zones if not z.mitigated]
    active_zones = active_zones[-max_zones:]

    # Check current candle for new FVG
    bullish_fvg = False
    bearish_fvg = False
    fvg_top = 0.0
    fvg_bottom = 0.0

    if len(candles) >= 3:
        # Check for bullish FVG on current candle
        if lows[-1] > highs[-3]:
            gap_size = lows[-1] - highs[-3]
            if gap_size >= min_gap_size:
                bullish_fvg = True
                fvg_top = lows[-1]
                fvg_bottom = highs[-3]

        # Check for bearish FVG on current candle
        elif highs[-1] < lows[-3]:
            gap_size = lows[-3] - highs[-1]
            if gap_size >= min_gap_size:
                bearish_fvg = True
                fvg_top = lows[-3]
                fvg_bottom = highs[-1]

    # Check if current price is inside any active FVG
    price_in_bullish_fvg = False
    price_in_bearish_fvg = False
    bouncing_off_bullish_fvg = False
    bouncing_off_bearish_fvg = False

    for zone in active_zones:
        if zone.type == 'bullish':
            # Price entered bullish FVG zone
            if current_low <= zone.top and current_low >= zone.bottom:
                price_in_bullish_fvg = True
                # Bouncing: entered zone but closed above it
                if current_close > zone.top:
                    bouncing_off_bullish_fvg = True
        else:
            # Price entered bearish FVG zone
            if current_high >= zone.bottom and current_high <= zone.top:
                price_in_bearish_fvg = True
                # Bouncing: entered zone but closed below it
                if current_close < zone.bottom:
                    bouncing_off_bearish_fvg = True

    return FVGResult(
        bullish_fvg=bullish_fvg,
        bearish_fvg=bearish_fvg,
        fvg_top=fvg_top,
        fvg_bottom=fvg_bottom,
        active_zones=active_zones,
        price_in_bullish_fvg=price_in_bullish_fvg,
        price_in_bearish_fvg=price_in_bearish_fvg,
        bouncing_off_bullish_fvg=bouncing_off_bullish_fvg,
        bouncing_off_bearish_fvg=bouncing_off_bearish_fvg
    )


def fvg_sequential(candles: np.ndarray, min_gap_atr_mult: float = 0.1) -> dict:
    """
    Calculate FVG indicators returning arrays for all candles.

    Args:
        candles: Jesse candle array
        min_gap_atr_mult: Minimum gap size as ATR multiple

    Returns:
        Dictionary with:
        - 'bullish_fvg': array of 1 (FVG) or 0 (no FVG)
        - 'bearish_fvg': array of 1 (FVG) or 0 (no FVG)
        - 'fvg_top': array of FVG top prices
        - 'fvg_bottom': array of FVG bottom prices
    """
    n = len(candles)

    if n < 3:
        return {
            'bullish_fvg': np.zeros(n),
            'bearish_fvg': np.zeros(n),
            'fvg_top': np.zeros(n),
            'fvg_bottom': np.zeros(n)
        }

    highs = candles[:, 3]
    lows = candles[:, 4]

    # Calculate ATR for minimum gap filter
    atr = _calculate_atr(candles, period=14)
    min_gap_size = atr * min_gap_atr_mult

    bullish_fvg = np.zeros(n)
    bearish_fvg = np.zeros(n)
    fvg_top = np.zeros(n)
    fvg_bottom = np.zeros(n)

    for i in range(2, n):
        # Bullish FVG
        if lows[i] > highs[i - 2]:
            gap_size = lows[i] - highs[i - 2]
            if gap_size >= min_gap_size:
                bullish_fvg[i] = 1
                fvg_top[i] = lows[i]
                fvg_bottom[i] = highs[i - 2]

        # Bearish FVG
        elif highs[i] < lows[i - 2]:
            gap_size = lows[i - 2] - highs[i]
            if gap_size >= min_gap_size:
                bearish_fvg[i] = 1
                fvg_top[i] = lows[i - 2]
                fvg_bottom[i] = highs[i]

    return {
        'bullish_fvg': bullish_fvg,
        'bearish_fvg': bearish_fvg,
        'fvg_top': fvg_top,
        'fvg_bottom': fvg_bottom
    }


def _calculate_atr(candles: np.ndarray, period: int = 14) -> float:
    """Calculate Average True Range"""
    if len(candles) < 2:
        return 0.0

    highs = candles[:, 3]
    lows = candles[:, 4]
    closes = candles[:, 2]

    tr = np.zeros(len(candles))
    tr[0] = highs[0] - lows[0]

    for i in range(1, len(candles)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr[i] = max(hl, hc, lc)

    if len(tr) < period:
        return np.mean(tr)

    return np.mean(tr[-period:])
