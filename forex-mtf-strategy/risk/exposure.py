"""
Exposure management for portfolio-level risk control.

Monitors and controls total exposure, correlation, and drawdown.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

from config.settings import get_settings
from execution.position_manager import Position, PositionManager
from monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ExposureStatus:
    """Current exposure status."""
    
    total_risk_amount: float
    total_risk_percent: float
    position_count: int
    can_open_new: bool
    available_risk: float
    max_risk_percent: float
    
    @property
    def risk_utilization(self) -> float:
        """Percentage of max risk currently used."""
        if self.max_risk_percent <= 0:
            return 0.0
        return self.total_risk_percent / self.max_risk_percent


@dataclass
class DrawdownStatus:
    """Drawdown monitoring status."""
    
    current_drawdown_pct: float
    max_drawdown_pct: float
    peak_balance: float
    current_balance: float
    is_max_drawdown_exceeded: bool


class ExposureManager:
    """
    Manage portfolio-level exposure and risk.
    
    Enforces:
    - Maximum total exposure
    - Maximum positions per instrument
    - Correlation limits
    - Drawdown limits
    """
    
    def __init__(
        self,
        account_balance: float = 10000.0,
        max_total_exposure: Optional[float] = None,
        max_positions_per_pair: int = 1,
        max_total_positions: int = 5,
        max_drawdown_pct: float = 0.20,
        correlation_threshold: float = 0.7,
    ):
        """
        Initialize exposure manager.
        
        Args:
            account_balance: Starting account balance
            max_total_exposure: Maximum total risk as decimal (default from settings)
            max_positions_per_pair: Max positions per instrument
            max_total_positions: Max total open positions
            max_drawdown_pct: Maximum drawdown before stopping
            correlation_threshold: Correlation limit between positions
        """
        settings = get_settings()
        
        self.account_balance = account_balance
        self.max_total_exposure = max_total_exposure or settings.risk.max_total_exposure
        self.max_positions_per_pair = max_positions_per_pair
        self.max_total_positions = max_total_positions
        self.max_drawdown_pct = max_drawdown_pct
        self.correlation_threshold = correlation_threshold
        
        # Track peak balance for drawdown
        self.peak_balance = account_balance
        
        # Correlated pairs (USD pairs move together, etc.)
        self.correlated_groups = {
            "USD_LONG": ["EUR_USD", "GBP_USD", "AUD_USD"],  # Short USD
            "USD_SHORT": ["USD_JPY", "USD_CHF", "USD_CAD"],  # Long USD
        }
    
    def get_exposure_status(
        self,
        position_manager: PositionManager,
    ) -> ExposureStatus:
        """
        Get current exposure status.
        
        Args:
            position_manager: Position manager with current positions
            
        Returns:
            ExposureStatus with current risk metrics
        """
        total_risk = position_manager.get_total_risk()
        risk_percent = total_risk / self.account_balance if self.account_balance > 0 else 0
        position_count = len(position_manager.open_positions)
        
        max_risk_amount = self.account_balance * self.max_total_exposure
        available_risk = max_risk_amount - total_risk
        
        can_open = (
            risk_percent < self.max_total_exposure and
            position_count < self.max_total_positions
        )
        
        return ExposureStatus(
            total_risk_amount=total_risk,
            total_risk_percent=risk_percent,
            position_count=position_count,
            can_open_new=can_open,
            available_risk=max(0, available_risk),
            max_risk_percent=self.max_total_exposure,
        )
    
    def can_open_position(
        self,
        instrument: str,
        risk_amount: float,
        position_manager: PositionManager,
    ) -> tuple[bool, str]:
        """
        Check if a new position can be opened.
        
        Args:
            instrument: Instrument for new position
            risk_amount: Risk amount for new position
            position_manager: Current position manager
            
        Returns:
            Tuple of (can_open, reason)
        """
        status = self.get_exposure_status(position_manager)
        
        # Check total exposure
        new_total_risk = status.total_risk_amount + risk_amount
        new_risk_pct = new_total_risk / self.account_balance
        
        if new_risk_pct > self.max_total_exposure:
            return False, f"Would exceed max exposure ({new_risk_pct:.1%} > {self.max_total_exposure:.1%})"
        
        # Check position count
        if status.position_count >= self.max_total_positions:
            return False, f"Max positions reached ({self.max_total_positions})"
        
        # Check positions per instrument
        instrument_positions = position_manager.get_position_by_instrument(instrument)
        if len(instrument_positions) >= self.max_positions_per_pair:
            return False, f"Max positions for {instrument} reached ({self.max_positions_per_pair})"
        
        # Check correlation
        correlation_issue = self._check_correlation(instrument, position_manager)
        if correlation_issue:
            return False, correlation_issue
        
        # Check drawdown
        dd_status = self.get_drawdown_status()
        if dd_status.is_max_drawdown_exceeded:
            return False, f"Max drawdown exceeded ({dd_status.current_drawdown_pct:.1%})"
        
        return True, "OK"
    
    def _check_correlation(
        self,
        new_instrument: str,
        position_manager: PositionManager,
    ) -> Optional[str]:
        """
        Check if new position would create too much correlated exposure.
        
        Args:
            new_instrument: Instrument to check
            position_manager: Current positions
            
        Returns:
            Error message if correlation issue, None otherwise
        """
        # Find which correlation group the new instrument belongs to
        new_group = None
        for group_name, instruments in self.correlated_groups.items():
            if new_instrument in instruments:
                new_group = group_name
                break
        
        if new_group is None:
            return None
        
        # Count existing positions in same group
        correlated_count = 0
        for position in position_manager.open_positions:
            for group_name, instruments in self.correlated_groups.items():
                if position.instrument in instruments and group_name == new_group:
                    correlated_count += 1
        
        # Allow up to 2 correlated positions
        if correlated_count >= 2:
            return f"Too many correlated positions in {new_group} group"
        
        return None
    
    def get_drawdown_status(self) -> DrawdownStatus:
        """
        Get current drawdown status.
        
        Returns:
            DrawdownStatus with drawdown metrics
        """
        # Update peak if balance increased
        if self.account_balance > self.peak_balance:
            self.peak_balance = self.account_balance
        
        drawdown = 0.0
        if self.peak_balance > 0:
            drawdown = (self.peak_balance - self.account_balance) / self.peak_balance
        
        return DrawdownStatus(
            current_drawdown_pct=drawdown,
            max_drawdown_pct=self.max_drawdown_pct,
            peak_balance=self.peak_balance,
            current_balance=self.account_balance,
            is_max_drawdown_exceeded=drawdown >= self.max_drawdown_pct,
        )
    
    def update_balance(self, new_balance: float):
        """
        Update account balance.
        
        Args:
            new_balance: New account balance
        """
        old_balance = self.account_balance
        self.account_balance = new_balance
        
        # Update peak
        if new_balance > self.peak_balance:
            self.peak_balance = new_balance
            logger.info(f"New peak balance: ${new_balance:.2f}")
        
        change = new_balance - old_balance
        change_pct = change / old_balance if old_balance > 0 else 0
        
        logger.info(
            f"Balance updated: ${old_balance:.2f} -> ${new_balance:.2f} "
            f"({change_pct:+.2%})"
        )
    
    def get_position_size_limit(
        self,
        instrument: str,
        position_manager: PositionManager,
        desired_risk_amount: float,
    ) -> float:
        """
        Get the maximum risk amount allowed for a new position.
        
        Args:
            instrument: Target instrument
            position_manager: Current positions
            desired_risk_amount: Desired risk for new position
            
        Returns:
            Allowed risk amount (may be less than desired)
        """
        status = self.get_exposure_status(position_manager)
        
        # Can't exceed available risk
        allowed = min(desired_risk_amount, status.available_risk)
        
        if allowed < desired_risk_amount:
            logger.warning(
                f"Risk reduced from ${desired_risk_amount:.2f} to ${allowed:.2f} "
                f"due to exposure limits"
            )
        
        return allowed
    
    def should_reduce_exposure(
        self,
        position_manager: PositionManager,
    ) -> bool:
        """
        Check if exposure should be reduced due to drawdown.
        
        Returns:
            True if positions should be reduced
        """
        dd_status = self.get_drawdown_status()
        
        # Start reducing at 50% of max drawdown
        reduce_threshold = self.max_drawdown_pct * 0.5
        
        return dd_status.current_drawdown_pct >= reduce_threshold
    
    def get_exposure_report(
        self,
        position_manager: PositionManager,
    ) -> str:
        """
        Generate a human-readable exposure report.
        
        Args:
            position_manager: Current positions
            
        Returns:
            Formatted report string
        """
        exp_status = self.get_exposure_status(position_manager)
        dd_status = self.get_drawdown_status()
        
        lines = [
            "=" * 50,
            "EXPOSURE REPORT",
            "=" * 50,
            f"Account Balance: ${self.account_balance:.2f}",
            f"Peak Balance:    ${dd_status.peak_balance:.2f}",
            f"Current DD:      {dd_status.current_drawdown_pct:.2%}",
            f"Max DD Limit:    {dd_status.max_drawdown_pct:.2%}",
            "-" * 50,
            f"Open Positions:  {exp_status.position_count}/{self.max_total_positions}",
            f"Total Risk:      ${exp_status.total_risk_amount:.2f} ({exp_status.total_risk_percent:.2%})",
            f"Available Risk:  ${exp_status.available_risk:.2f}",
            f"Can Open New:    {'Yes' if exp_status.can_open_new else 'No'}",
            "=" * 50,
        ]
        
        return "\n".join(lines)
