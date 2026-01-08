"""
Stop loss calculation methods.

Implements ATR-based, structure-based, and percentage-based stop losses.
"""

from dataclasses import dataclass
from typing import Optional

import pandas as pd
import pandas_ta as ta

from monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class StopLevels:
    """Stop loss and take profit levels."""
    
    stop_loss: float
    take_profit: Optional[float] = None
    trailing_stop: Optional[float] = None
    breakeven_trigger: Optional[float] = None
    
    @property
    def risk_reward(self) -> Optional[float]:
        """Calculate risk/reward ratio."""
        if self.take_profit is None:
            return None
        # This is calculated relative to entry, so requires entry price
        return None


class StopLossCalculator:
    """
    Calculate stop loss levels using various methods.
    
    Methods:
    - ATR-based: Stop at N x ATR from entry
    - Structure-based: Stop below/above recent swing
    - Percentage-based: Fixed percentage from entry
    - FVG-based: Stop below/above FVG zone
    """
    
    def __init__(
        self,
        atr_multiplier: float = 1.5,
        atr_period: int = 14,
        default_rr_ratio: float = 2.0,
        min_stop_pips: float = 10.0,
        max_stop_pips: float = 50.0,
    ):
        """
        Initialize stop loss calculator.
        
        Args:
            atr_multiplier: Multiplier for ATR-based stops
            atr_period: Period for ATR calculation
            default_rr_ratio: Default risk/reward ratio for TP
            min_stop_pips: Minimum stop distance in pips
            max_stop_pips: Maximum stop distance in pips
        """
        self.atr_multiplier = atr_multiplier
        self.atr_period = atr_period
        self.default_rr_ratio = default_rr_ratio
        self.min_stop_pips = min_stop_pips
        self.max_stop_pips = max_stop_pips
        self.pip_size = 0.0001
    
    def calculate_atr_stop(
        self,
        df: pd.DataFrame,
        entry_price: float,
        is_long: bool,
        multiplier: Optional[float] = None,
    ) -> StopLevels:
        """
        Calculate stop loss based on ATR.
        
        Args:
            df: DataFrame with OHLC data
            entry_price: Entry price
            is_long: True for long positions
            multiplier: ATR multiplier (overrides default)
            
        Returns:
            StopLevels with ATR-based stop
        """
        mult = multiplier or self.atr_multiplier
        
        # Calculate ATR
        atr = ta.atr(df["high"], df["low"], df["close"], length=self.atr_period)
        current_atr = atr.iloc[-1]
        
        # Calculate stop distance
        stop_distance = current_atr * mult
        
        # Enforce min/max
        stop_distance = max(stop_distance, self.min_stop_pips * self.pip_size)
        stop_distance = min(stop_distance, self.max_stop_pips * self.pip_size)
        
        if is_long:
            stop_loss = entry_price - stop_distance
            take_profit = entry_price + (stop_distance * self.default_rr_ratio)
        else:
            stop_loss = entry_price + stop_distance
            take_profit = entry_price - (stop_distance * self.default_rr_ratio)
        
        logger.debug(
            f"ATR stop: {stop_loss:.5f} (ATR: {current_atr:.5f}, "
            f"distance: {stop_distance/self.pip_size:.1f} pips)"
        )
        
        return StopLevels(
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
    
    def calculate_structure_stop(
        self,
        df: pd.DataFrame,
        entry_price: float,
        is_long: bool,
        lookback: int = 20,
        buffer_pips: float = 5.0,
    ) -> StopLevels:
        """
        Calculate stop loss based on recent swing high/low.
        
        Args:
            df: DataFrame with OHLC data
            entry_price: Entry price
            is_long: True for long positions
            lookback: Bars to look back for swing
            buffer_pips: Buffer pips beyond swing
            
        Returns:
            StopLevels with structure-based stop
        """
        recent_df = df.iloc[-lookback:]
        buffer = buffer_pips * self.pip_size
        
        if is_long:
            # Stop below recent swing low
            swing_low = recent_df["low"].min()
            stop_loss = swing_low - buffer
        else:
            # Stop above recent swing high
            swing_high = recent_df["high"].max()
            stop_loss = swing_high + buffer
        
        # Check if within acceptable range
        stop_distance = abs(entry_price - stop_loss)
        stop_pips = stop_distance / self.pip_size
        
        if stop_pips < self.min_stop_pips:
            logger.warning(f"Structure stop too tight ({stop_pips:.1f} pips), using minimum")
            if is_long:
                stop_loss = entry_price - (self.min_stop_pips * self.pip_size)
            else:
                stop_loss = entry_price + (self.min_stop_pips * self.pip_size)
        elif stop_pips > self.max_stop_pips:
            logger.warning(f"Structure stop too wide ({stop_pips:.1f} pips), using ATR-based")
            return self.calculate_atr_stop(df, entry_price, is_long)
        
        # Calculate take profit
        stop_distance = abs(entry_price - stop_loss)
        if is_long:
            take_profit = entry_price + (stop_distance * self.default_rr_ratio)
        else:
            take_profit = entry_price - (stop_distance * self.default_rr_ratio)
        
        return StopLevels(
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
    
    def calculate_fvg_stop(
        self,
        entry_price: float,
        fvg_top: float,
        fvg_bottom: float,
        is_long: bool,
        buffer_pips: float = 3.0,
    ) -> StopLevels:
        """
        Calculate stop loss based on FVG zone.
        
        Args:
            entry_price: Entry price
            fvg_top: FVG zone top
            fvg_bottom: FVG zone bottom
            is_long: True for long positions
            buffer_pips: Buffer pips beyond FVG
            
        Returns:
            StopLevels with FVG-based stop
        """
        buffer = buffer_pips * self.pip_size
        
        if is_long:
            # Stop below FVG zone
            stop_loss = fvg_bottom - buffer
        else:
            # Stop above FVG zone
            stop_loss = fvg_top + buffer
        
        # Calculate take profit
        stop_distance = abs(entry_price - stop_loss)
        if is_long:
            take_profit = entry_price + (stop_distance * self.default_rr_ratio)
        else:
            take_profit = entry_price - (stop_distance * self.default_rr_ratio)
        
        return StopLevels(
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
    
    def calculate_percentage_stop(
        self,
        entry_price: float,
        is_long: bool,
        stop_percent: float = 0.5,
    ) -> StopLevels:
        """
        Calculate fixed percentage stop loss.
        
        Args:
            entry_price: Entry price
            is_long: True for long positions
            stop_percent: Stop loss percentage (0.5 = 0.5%)
            
        Returns:
            StopLevels with percentage-based stop
        """
        stop_distance = entry_price * (stop_percent / 100)
        
        if is_long:
            stop_loss = entry_price - stop_distance
            take_profit = entry_price + (stop_distance * self.default_rr_ratio)
        else:
            stop_loss = entry_price + stop_distance
            take_profit = entry_price - (stop_distance * self.default_rr_ratio)
        
        return StopLevels(
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
    
    def calculate_breakeven_trigger(
        self,
        entry_price: float,
        stop_loss: float,
        is_long: bool,
        trigger_multiple: float = 1.0,
    ) -> float:
        """
        Calculate price at which to move stop to breakeven.
        
        Args:
            entry_price: Entry price
            stop_loss: Current stop loss
            is_long: True for long positions
            trigger_multiple: Multiple of risk to trigger BE (1.0 = 1R)
            
        Returns:
            Price level to trigger breakeven
        """
        risk = abs(entry_price - stop_loss)
        
        if is_long:
            return entry_price + (risk * trigger_multiple)
        else:
            return entry_price - (risk * trigger_multiple)
    
    def calculate_trailing_stop(
        self,
        current_price: float,
        highest_price: float,  # Highest since entry (for long)
        lowest_price: float,   # Lowest since entry (for short)
        is_long: bool,
        trail_pips: float = 20.0,
    ) -> float:
        """
        Calculate trailing stop level.
        
        Args:
            current_price: Current price
            highest_price: Highest price since entry (for long)
            lowest_price: Lowest price since entry (for short)
            is_long: True for long positions
            trail_pips: Trailing distance in pips
            
        Returns:
            Trailing stop price
        """
        trail_distance = trail_pips * self.pip_size
        
        if is_long:
            return highest_price - trail_distance
        else:
            return lowest_price + trail_distance
