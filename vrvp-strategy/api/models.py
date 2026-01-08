"""Pydantic models for API responses"""
from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime
from enum import Enum


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class PairStatus(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"
    STARTING = "starting"


class SignalTypeEnum(str, Enum):
    NONE = "NONE"
    LONG = "LONG"
    SHORT = "SHORT"
    EXIT_LONG = "EXIT_LONG"
    EXIT_SHORT = "EXIT_SHORT"


class HealthResponse(BaseModel):
    status: HealthStatus
    timestamp: datetime
    pairs_running: int
    pairs_total: int
    authenticated: bool


class SignalResponse(BaseModel):
    instrument: str
    signal_type: SignalTypeEnum
    price: float
    strength: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reasons: List[str] = []
    timestamp: datetime


class PairStatusResponse(BaseModel):
    instrument: str
    status: PairStatus
    last_signal: Optional[SignalResponse] = None
    last_update: Optional[datetime] = None
    error_message: Optional[str] = None
    candles_ltf: int = 0
    candles_htf: int = 0


class StrategyStatusResponse(BaseModel):
    status: str
    running: bool
    authenticated: bool
    environment: str
    pairs: List[PairStatusResponse]
    fetch_interval_minutes: int
    timeframe: str
    htf_timeframe: str
    started_at: Optional[datetime] = None


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None
