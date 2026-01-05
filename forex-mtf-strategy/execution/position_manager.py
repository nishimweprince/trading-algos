"""
Position management for tracking and managing open trades.

Provides a unified interface for managing positions across
live trading and backtesting.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

import pandas as pd

from monitoring.logger import get_logger

logger = get_logger(__name__)


class PositionStatus(Enum):
    """Position status enumeration."""
    
    PENDING = "pending"
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class PositionSide(Enum):
    """Position side enumeration."""
    
    LONG = "long"
    SHORT = "short"


@dataclass
class Position:
    """Represents a trading position."""
    
    id: str
    instrument: str
    side: PositionSide
    units: int
    entry_price: float
    entry_time: datetime
    
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    
    status: PositionStatus = PositionStatus.OPEN
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    
    # Risk metrics
    risk_amount: float = 0.0  # Risk in account currency
    risk_reward_ratio: float = 0.0
    
    # Performance
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    
    # Metadata
    signal_strength: int = 0
    notes: str = ""
    
    @property
    def is_long(self) -> bool:
        """Check if position is long."""
        return self.side == PositionSide.LONG
    
    @property
    def is_short(self) -> bool:
        """Check if position is short."""
        return self.side == PositionSide.SHORT
    
    @property
    def is_open(self) -> bool:
        """Check if position is open."""
        return self.status == PositionStatus.OPEN
    
    @property
    def duration(self) -> Optional[pd.Timedelta]:
        """Get position duration."""
        if self.exit_time:
            return self.exit_time - self.entry_time
        return datetime.now() - self.entry_time
    
    def update_pnl(self, current_price: float, pip_value: float = 10.0):
        """
        Update unrealized P&L based on current price.
        
        Args:
            current_price: Current market price
            pip_value: Value per pip per lot
        """
        price_diff = current_price - self.entry_price
        if self.is_short:
            price_diff = -price_diff
        
        pip_diff = price_diff / 0.0001  # For EUR/USD, etc.
        self.unrealized_pnl = pip_diff * (abs(self.units) / 100000) * pip_value
    
    def close(self, exit_price: float, exit_time: Optional[datetime] = None):
        """
        Close the position.
        
        Args:
            exit_price: Price at which position was closed
            exit_time: Time of close (defaults to now)
        """
        self.exit_price = exit_price
        self.exit_time = exit_time or datetime.now()
        self.status = PositionStatus.CLOSED
        
        # Calculate realized P&L
        price_diff = exit_price - self.entry_price
        if self.is_short:
            price_diff = -price_diff
        
        pip_diff = price_diff / 0.0001
        self.realized_pnl = pip_diff * (abs(self.units) / 100000) * 10  # Assuming $10/pip
        self.unrealized_pnl = 0.0
    
    def to_dict(self) -> dict:
        """Convert position to dictionary."""
        return {
            "id": self.id,
            "instrument": self.instrument,
            "side": self.side.value,
            "units": self.units,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time.isoformat(),
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "status": self.status.value,
            "exit_price": self.exit_price,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
        }


@dataclass
class PositionManager:
    """
    Manage trading positions.
    
    Tracks open and closed positions, provides position statistics,
    and handles position-level risk management.
    """
    
    positions: dict[str, Position] = field(default_factory=dict)
    _position_counter: int = field(default=0, repr=False)
    
    @property
    def open_positions(self) -> list[Position]:
        """Get list of open positions."""
        return [p for p in self.positions.values() if p.is_open]
    
    @property
    def closed_positions(self) -> list[Position]:
        """Get list of closed positions."""
        return [p for p in self.positions.values() if p.status == PositionStatus.CLOSED]
    
    def create_position(
        self,
        instrument: str,
        side: PositionSide,
        units: int,
        entry_price: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        entry_time: Optional[datetime] = None,
        signal_strength: int = 0,
    ) -> Position:
        """
        Create a new position.
        
        Args:
            instrument: Trading instrument
            side: Position side (long/short)
            units: Number of units
            entry_price: Entry price
            stop_loss: Stop loss price
            take_profit: Take profit price
            entry_time: Entry timestamp
            signal_strength: Signal strength (1-4)
            
        Returns:
            Created Position object
        """
        self._position_counter += 1
        position_id = f"POS_{self._position_counter:06d}"
        
        position = Position(
            id=position_id,
            instrument=instrument,
            side=side,
            units=abs(units),
            entry_price=entry_price,
            entry_time=entry_time or datetime.now(),
            stop_loss=stop_loss,
            take_profit=take_profit,
            signal_strength=signal_strength,
        )
        
        # Calculate risk amount if stop loss provided
        if stop_loss:
            sl_distance = abs(entry_price - stop_loss) / 0.0001  # Pips
            position.risk_amount = sl_distance * (abs(units) / 100000) * 10
            
            # Calculate R:R if take profit provided
            if take_profit:
                tp_distance = abs(take_profit - entry_price) / 0.0001
                position.risk_reward_ratio = tp_distance / sl_distance if sl_distance > 0 else 0
        
        self.positions[position_id] = position
        logger.info(f"Created position: {position_id} {side.value} {units} {instrument} @ {entry_price}")
        
        return position
    
    def close_position(
        self,
        position_id: str,
        exit_price: float,
        exit_time: Optional[datetime] = None,
    ) -> Optional[Position]:
        """
        Close a position.
        
        Args:
            position_id: Position ID to close
            exit_price: Exit price
            exit_time: Exit timestamp
            
        Returns:
            Closed Position object or None
        """
        position = self.positions.get(position_id)
        if not position:
            logger.warning(f"Position not found: {position_id}")
            return None
        
        if not position.is_open:
            logger.warning(f"Position already closed: {position_id}")
            return position
        
        position.close(exit_price, exit_time)
        logger.info(f"Closed position: {position_id} @ {exit_price}, PnL: {position.realized_pnl:.2f}")
        
        return position
    
    def update_position(
        self,
        position_id: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        current_price: Optional[float] = None,
    ) -> Optional[Position]:
        """
        Update a position's parameters.
        
        Args:
            position_id: Position ID to update
            stop_loss: New stop loss price
            take_profit: New take profit price
            current_price: Current price for P&L update
            
        Returns:
            Updated Position or None
        """
        position = self.positions.get(position_id)
        if not position:
            return None
        
        if stop_loss is not None:
            position.stop_loss = stop_loss
        
        if take_profit is not None:
            position.take_profit = take_profit
        
        if current_price is not None:
            position.update_pnl(current_price)
        
        return position
    
    def check_stops(
        self,
        instrument: str,
        current_high: float,
        current_low: float,
        current_time: datetime,
    ) -> list[Position]:
        """
        Check if any positions hit their stops.
        
        Args:
            instrument: Instrument to check
            current_high: Current candle high
            current_low: Current candle low
            current_time: Current time
            
        Returns:
            List of positions that were closed
        """
        closed = []
        
        for position in self.open_positions:
            if position.instrument != instrument:
                continue
            
            exit_price = None
            
            # Check stop loss
            if position.stop_loss:
                if position.is_long and current_low <= position.stop_loss:
                    exit_price = position.stop_loss
                elif position.is_short and current_high >= position.stop_loss:
                    exit_price = position.stop_loss
            
            # Check take profit (only if not stopped out)
            if exit_price is None and position.take_profit:
                if position.is_long and current_high >= position.take_profit:
                    exit_price = position.take_profit
                elif position.is_short and current_low <= position.take_profit:
                    exit_price = position.take_profit
            
            if exit_price is not None:
                position.close(exit_price, current_time)
                closed.append(position)
        
        return closed
    
    def get_position_by_instrument(self, instrument: str) -> list[Position]:
        """Get all open positions for an instrument."""
        return [p for p in self.open_positions if p.instrument == instrument]
    
    def get_total_exposure(self, instrument: Optional[str] = None) -> int:
        """
        Get total units exposed.
        
        Args:
            instrument: Filter by instrument (optional)
            
        Returns:
            Net units (positive=net long, negative=net short)
        """
        positions = self.open_positions
        if instrument:
            positions = [p for p in positions if p.instrument == instrument]
        
        total = 0
        for p in positions:
            if p.is_long:
                total += p.units
            else:
                total -= p.units
        
        return total
    
    def get_total_risk(self) -> float:
        """Get total risk amount of all open positions."""
        return sum(p.risk_amount for p in self.open_positions)
    
    def get_statistics(self) -> dict:
        """
        Get position statistics.
        
        Returns:
            Dict with position statistics
        """
        closed = self.closed_positions
        
        if not closed:
            return {
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "profit_factor": 0.0,
                "avg_duration": None,
            }
        
        winners = [p for p in closed if p.realized_pnl > 0]
        losers = [p for p in closed if p.realized_pnl < 0]
        
        total_wins = sum(p.realized_pnl for p in winners)
        total_losses = abs(sum(p.realized_pnl for p in losers))
        
        avg_duration = sum(
            (p.duration.total_seconds() for p in closed if p.duration),
            start=0.0
        ) / len(closed)
        
        return {
            "total_trades": len(closed),
            "winning_trades": len(winners),
            "losing_trades": len(losers),
            "win_rate": len(winners) / len(closed) if closed else 0.0,
            "total_pnl": sum(p.realized_pnl for p in closed),
            "avg_win": total_wins / len(winners) if winners else 0.0,
            "avg_loss": total_losses / len(losers) if losers else 0.0,
            "profit_factor": total_wins / total_losses if total_losses > 0 else float('inf'),
            "avg_duration_hours": avg_duration / 3600,
            "open_positions": len(self.open_positions),
            "total_open_pnl": sum(p.unrealized_pnl for p in self.open_positions),
        }
    
    def to_dataframe(self) -> pd.DataFrame:
        """
        Export all positions to DataFrame.
        
        Returns:
            DataFrame with all position data
        """
        if not self.positions:
            return pd.DataFrame()
        
        data = [p.to_dict() for p in self.positions.values()]
        df = pd.DataFrame(data)
        df["entry_time"] = pd.to_datetime(df["entry_time"])
        if "exit_time" in df.columns:
            df["exit_time"] = pd.to_datetime(df["exit_time"])
        
        return df
