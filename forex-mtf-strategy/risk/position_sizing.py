"""
Position sizing calculations for risk management.

Implements fixed percentage risk and Kelly criterion methods.
"""

from dataclasses import dataclass
from typing import Optional

from config.settings import get_settings
from monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PositionSize:
    """Position size calculation result."""
    
    units: int
    risk_amount: float
    risk_percent: float
    pip_value: float
    stop_distance_pips: float


class PositionSizer:
    """
    Calculate position sizes based on risk parameters.
    
    Supports:
    - Fixed percentage risk (most common)
    - Fixed position size
    - Kelly criterion (for optimal growth)
    """
    
    def __init__(
        self,
        account_balance: Optional[float] = None,
        max_risk_pct: Optional[float] = None,
        max_position_size: Optional[float] = None,
        pip_value: Optional[float] = None,
    ):
        """
        Initialize position sizer.
        
        Args:
            account_balance: Account balance in base currency
            max_risk_pct: Maximum risk per trade as decimal (e.g., 0.02 = 2%)
            max_position_size: Maximum position size in units
            pip_value: Value per pip per standard lot
        """
        settings = get_settings()
        
        self.account_balance = account_balance or 10000.0
        self.max_risk_pct = max_risk_pct or settings.risk.max_risk_per_trade
        self.max_position_size = max_position_size or settings.risk.max_position_size
        self.default_pip_value = pip_value or settings.risk.default_pip_value
        self.pip_size = 0.0001  # For EUR/USD, GBP/USD, etc.
    
    def calculate_fixed_risk(
        self,
        entry_price: float,
        stop_loss: float,
        risk_percent: Optional[float] = None,
        pip_value: Optional[float] = None,
    ) -> PositionSize:
        """
        Calculate position size for fixed percentage risk.
        
        Args:
            entry_price: Planned entry price
            stop_loss: Stop loss price
            risk_percent: Risk as decimal (overrides default)
            pip_value: Pip value per lot (overrides default)
            
        Returns:
            PositionSize with calculated values
        """
        risk_pct = risk_percent or self.max_risk_pct
        pv = pip_value or self.default_pip_value
        
        # Calculate risk amount
        risk_amount = self.account_balance * risk_pct
        
        # Calculate stop distance in pips
        stop_distance = abs(entry_price - stop_loss)
        stop_distance_pips = stop_distance / self.pip_size
        
        if stop_distance_pips <= 0:
            logger.warning("Stop distance is zero or negative")
            return PositionSize(
                units=0,
                risk_amount=0,
                risk_percent=risk_pct,
                pip_value=pv,
                stop_distance_pips=0,
            )
        
        # Calculate position size
        # risk_amount = pip_distance * (units / 100000) * pip_value
        # units = risk_amount * 100000 / (pip_distance * pip_value)
        units = (risk_amount * 100000) / (stop_distance_pips * pv)
        
        # Apply maximum position size cap
        units = min(units, self.max_position_size)
        
        # Round to nearest 1000 (micro lot)
        units = int(units / 1000) * 1000
        units = max(units, 1000)  # Minimum 1 micro lot
        
        # Recalculate actual risk with rounded position
        actual_risk = (stop_distance_pips * (units / 100000) * pv)
        
        result = PositionSize(
            units=units,
            risk_amount=actual_risk,
            risk_percent=actual_risk / self.account_balance,
            pip_value=pv,
            stop_distance_pips=stop_distance_pips,
        )
        
        logger.debug(
            f"Position size: {units} units, "
            f"risk: ${actual_risk:.2f} ({result.risk_percent:.2%}), "
            f"stop: {stop_distance_pips:.1f} pips"
        )
        
        return result
    
    def calculate_fixed_size(
        self,
        units: int,
        entry_price: float,
        stop_loss: float,
        pip_value: Optional[float] = None,
    ) -> PositionSize:
        """
        Calculate risk for a fixed position size.
        
        Args:
            units: Fixed position size
            entry_price: Entry price
            stop_loss: Stop loss price
            pip_value: Pip value per lot
            
        Returns:
            PositionSize with risk calculation
        """
        pv = pip_value or self.default_pip_value
        
        stop_distance = abs(entry_price - stop_loss)
        stop_distance_pips = stop_distance / self.pip_size
        
        risk_amount = stop_distance_pips * (units / 100000) * pv
        risk_percent = risk_amount / self.account_balance
        
        return PositionSize(
            units=units,
            risk_amount=risk_amount,
            risk_percent=risk_percent,
            pip_value=pv,
            stop_distance_pips=stop_distance_pips,
        )
    
    def calculate_kelly(
        self,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        entry_price: float,
        stop_loss: float,
        kelly_fraction: float = 0.25,  # Use fraction of Kelly for safety
    ) -> PositionSize:
        """
        Calculate position size using Kelly criterion.
        
        Kelly formula: f = (bp - q) / b
        where:
            f = fraction of capital to bet
            b = ratio of avg win to avg loss
            p = probability of winning
            q = probability of losing (1 - p)
        
        Args:
            win_rate: Historical win rate (0-1)
            avg_win: Average winning trade amount
            avg_loss: Average losing trade amount
            entry_price: Entry price
            stop_loss: Stop loss price
            kelly_fraction: Fraction of Kelly to use (reduces risk)
            
        Returns:
            PositionSize based on Kelly criterion
        """
        if avg_loss <= 0 or win_rate <= 0:
            logger.warning("Invalid parameters for Kelly calculation")
            return self.calculate_fixed_risk(entry_price, stop_loss)
        
        # Calculate Kelly percentage
        b = avg_win / avg_loss  # Win/loss ratio
        p = win_rate
        q = 1 - p
        
        kelly = (b * p - q) / b
        
        # Cap at max risk and apply fraction
        kelly = max(0, min(kelly * kelly_fraction, self.max_risk_pct))
        
        logger.debug(f"Kelly criterion: {kelly:.2%} (full Kelly: {kelly/kelly_fraction:.2%})")
        
        return self.calculate_fixed_risk(
            entry_price=entry_price,
            stop_loss=stop_loss,
            risk_percent=kelly,
        )
    
    def update_balance(self, new_balance: float):
        """Update account balance for sizing calculations."""
        self.account_balance = new_balance
        logger.info(f"Updated account balance: ${new_balance:.2f}")
    
    def get_max_position_for_risk(
        self,
        stop_distance_pips: float,
        pip_value: Optional[float] = None,
    ) -> int:
        """
        Get maximum position size for given stop distance.
        
        Args:
            stop_distance_pips: Stop loss distance in pips
            pip_value: Pip value per lot
            
        Returns:
            Maximum position size in units
        """
        pv = pip_value or self.default_pip_value
        risk_amount = self.account_balance * self.max_risk_pct
        
        if stop_distance_pips <= 0:
            return 0
        
        units = (risk_amount * 100000) / (stop_distance_pips * pv)
        return min(int(units), int(self.max_position_size))
