"""Data Transfer Objects (DTOs) for normalized data structures across all data sources"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class CandleDTO:
    """Standardized OHLCV candle data"""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class PriceDTO:
    """Current bid/ask price data"""
    bid: float
    ask: float
    mid: float
    spread: float
    timestamp: datetime


@dataclass
class AccountDTO:
    """Account information"""
    balance: float
    equity: float
    margin_available: float
    margin_used: float
    unrealized_pnl: float


@dataclass
class OrderDTO:
    """Order information"""
    order_id: str
    instrument: str
    direction: int  # 1 for long, -1 for short
    units: int
    price: float
    status: str


@dataclass
class TradeDTO:
    """Open position/trade information"""
    trade_id: str
    instrument: str
    direction: int  # 1 for long, -1 for short
    units: int
    entry_price: float
    current_price: float
    unrealized_pnl: float
    stop_loss: Optional[float]
    take_profit: Optional[float]

