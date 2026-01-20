"""
Trading filters for signal validation.

Implements time-based, spread, and other filters to ensure
signals are only generated during optimal trading conditions.
"""

from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional

import pandas as pd

from monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TradingSession:
    """Represents a forex trading session."""
    
    name: str
    start: time
    end: time
    
    def is_active(self, current_time: time) -> bool:
        """Check if session is active at given time."""
        if self.start <= self.end:
            return self.start <= current_time <= self.end
        else:  # Wraps midnight
            return current_time >= self.start or current_time <= self.end


# Standard forex sessions (UTC)
TOKYO_SESSION = TradingSession("Tokyo", time(0, 0), time(9, 0))
LONDON_SESSION = TradingSession("London", time(7, 0), time(16, 0))
NEW_YORK_SESSION = TradingSession("New York", time(12, 0), time(21, 0))
LONDON_NY_OVERLAP = TradingSession("London-NY Overlap", time(12, 0), time(16, 0))


class TimeFilter:
    """
    Filter trades based on time and trading sessions.
    
    Forex markets have different characteristics during different sessions.
    The London-New York overlap typically has the highest liquidity.
    """
    
    def __init__(
        self,
        allowed_sessions: Optional[list[TradingSession]] = None,
        excluded_days: Optional[list[int]] = None,  # 0=Monday, 6=Sunday
        excluded_hours: Optional[list[int]] = None,
    ):
        """
        Initialize time filter.
        
        Args:
            allowed_sessions: Trading sessions to allow (None = all)
            excluded_days: Days of week to exclude (0-6)
            excluded_hours: Hours to exclude (0-23)
        """
        self.allowed_sessions = allowed_sessions
        self.excluded_days = excluded_days or [5, 6]  # Sat, Sun by default
        self.excluded_hours = excluded_hours or []
    
    def is_allowed(self, timestamp: pd.Timestamp) -> bool:
        """
        Check if trading is allowed at given timestamp.
        
        Args:
            timestamp: Timestamp to check
            
        Returns:
            True if trading is allowed
        """
        # Check excluded days
        if timestamp.dayofweek in self.excluded_days:
            return False
        
        # Check excluded hours
        if timestamp.hour in self.excluded_hours:
            return False
        
        # Check allowed sessions
        if self.allowed_sessions:
            current_time = timestamp.time()
            for session in self.allowed_sessions:
                if session.is_active(current_time):
                    return True
            return False
        
        return True
    
    def get_current_session(self, timestamp: pd.Timestamp) -> Optional[str]:
        """
        Get the name of the current trading session.
        
        Args:
            timestamp: Timestamp to check
            
        Returns:
            Session name or None if no session active
        """
        current_time = timestamp.time()
        
        sessions = [TOKYO_SESSION, LONDON_SESSION, NEW_YORK_SESSION]
        for session in sessions:
            if session.is_active(current_time):
                return session.name
        
        return None
    
    def filter_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Filter DataFrame to only include allowed times.
        
        Args:
            df: DataFrame with DatetimeIndex
            
        Returns:
            Filtered DataFrame
        """
        mask = df.index.map(self.is_allowed)
        return df[mask]


class SpreadFilter:
    """
    Filter trades based on spread conditions.
    
    High spreads during low liquidity periods can significantly
    impact trade profitability.
    """
    
    def __init__(
        self,
        max_spread_pips: float = 3.0,
        max_spread_atr_ratio: float = 0.1,
    ):
        """
        Initialize spread filter.
        
        Args:
            max_spread_pips: Maximum allowed spread in pips
            max_spread_atr_ratio: Maximum spread as ratio of ATR
        """
        self.max_spread_pips = max_spread_pips
        self.max_spread_atr_ratio = max_spread_atr_ratio
        self.pip_size = 0.0001  # For EUR/USD, etc.
    
    def is_allowed(
        self,
        spread: float,
        atr: Optional[float] = None,
    ) -> bool:
        """
        Check if spread is acceptable.
        
        Args:
            spread: Current spread in price units
            atr: Current ATR value (optional)
            
        Returns:
            True if spread is acceptable
        """
        spread_pips = spread / self.pip_size
        
        if spread_pips > self.max_spread_pips:
            return False
        
        if atr is not None and atr > 0:
            if spread / atr > self.max_spread_atr_ratio:
                return False
        
        return True
    
    def get_adjusted_entry(
        self,
        signal_price: float,
        spread: float,
        is_buy: bool,
    ) -> float:
        """
        Get spread-adjusted entry price.
        
        Args:
            signal_price: Signal price (typically mid)
            spread: Current spread
            is_buy: True for buy, False for sell
            
        Returns:
            Adjusted entry price
        """
        half_spread = spread / 2
        
        if is_buy:
            return signal_price + half_spread  # Buy at ask
        else:
            return signal_price - half_spread  # Sell at bid


class VolatilityFilter:
    """
    Filter trades based on volatility conditions.
    
    Extremely high or low volatility periods may not be suitable
    for this strategy.
    """
    
    def __init__(
        self,
        min_atr_pips: float = 5.0,
        max_atr_pips: float = 50.0,
    ):
        """
        Initialize volatility filter.
        
        Args:
            min_atr_pips: Minimum ATR in pips
            max_atr_pips: Maximum ATR in pips
        """
        self.min_atr_pips = min_atr_pips
        self.max_atr_pips = max_atr_pips
        self.pip_size = 0.0001
    
    def is_allowed(self, atr: float) -> bool:
        """
        Check if volatility is acceptable.
        
        Args:
            atr: Current ATR value
            
        Returns:
            True if volatility is acceptable
        """
        atr_pips = atr / self.pip_size
        return self.min_atr_pips <= atr_pips <= self.max_atr_pips


class TradingFilters:
    """
    Combined trading filters.
    
    Aggregates multiple filters for comprehensive signal validation.
    """
    
    def __init__(
        self,
        time_filter: Optional[TimeFilter] = None,
        spread_filter: Optional[SpreadFilter] = None,
        volatility_filter: Optional[VolatilityFilter] = None,
    ):
        """
        Initialize combined filters.
        
        Args:
            time_filter: Time-based filter
            spread_filter: Spread-based filter
            volatility_filter: Volatility-based filter
        """
        self.time_filter = time_filter or TimeFilter()
        self.spread_filter = spread_filter or SpreadFilter()
        self.volatility_filter = volatility_filter or VolatilityFilter()
    
    def validate_signal(
        self,
        timestamp: pd.Timestamp,
        spread: Optional[float] = None,
        atr: Optional[float] = None,
    ) -> tuple[bool, list[str]]:
        """
        Validate a signal against all filters.
        
        Args:
            timestamp: Signal timestamp
            spread: Current spread (optional)
            atr: Current ATR (optional)
            
        Returns:
            Tuple of (is_valid, list of rejection reasons)
        """
        rejections = []
        
        # Time filter
        if not self.time_filter.is_allowed(timestamp):
            rejections.append(f"Time filter: trading not allowed at {timestamp}")
        
        # Spread filter
        if spread is not None and not self.spread_filter.is_allowed(spread, atr):
            rejections.append(f"Spread filter: spread {spread} too high")
        
        # Volatility filter
        if atr is not None and not self.volatility_filter.is_allowed(atr):
            rejections.append(f"Volatility filter: ATR {atr} outside range")
        
        is_valid = len(rejections) == 0
        
        if not is_valid:
            logger.debug(f"Signal rejected: {', '.join(rejections)}")
        
        return is_valid, rejections
    
    @classmethod
    def create_default(cls) -> "TradingFilters":
        """
        Create filters with default settings.
        
        Returns:
            TradingFilters with sensible defaults
        """
        return cls(
            time_filter=TimeFilter(
                excluded_days=[5, 6],  # Weekend
                excluded_hours=[22, 23, 0, 1],  # Low liquidity hours
            ),
            spread_filter=SpreadFilter(max_spread_pips=3.0),
            volatility_filter=VolatilityFilter(min_atr_pips=5.0, max_atr_pips=50.0),
        )
    
    @classmethod
    def create_strict(cls) -> "TradingFilters":
        """
        Create strict filters for conservative trading.
        
        Returns:
            TradingFilters with strict settings
        """
        return cls(
            time_filter=TimeFilter(
                allowed_sessions=[LONDON_NY_OVERLAP],  # Only overlap
                excluded_days=[4, 5, 6],  # Fri, Sat, Sun
            ),
            spread_filter=SpreadFilter(max_spread_pips=2.0),
            volatility_filter=VolatilityFilter(min_atr_pips=8.0, max_atr_pips=30.0),
        )
