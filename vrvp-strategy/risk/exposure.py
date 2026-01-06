"""Exposure Manager - portfolio-level risk"""
from dataclasses import dataclass
from typing import Dict, Optional
from datetime import datetime
from loguru import logger
from config import RiskConfig

@dataclass
class Position:
    instrument: str
    direction: int
    units: int
    entry_price: float
    stop_loss: float
    take_profit: float
    entry_time: datetime
    risk_amount: float

@dataclass
class ExposureReport:
    total_positions: int
    total_units: int
    total_risk: float
    risk_pct_of_balance: float
    max_drawdown_pct: float
    is_halted: bool
    halt_reason: Optional[str]

class ExposureManager:
    def __init__(self, config: RiskConfig = None, initial_balance: float = 10000):
        self.config = config or RiskConfig()
        self.initial_balance = initial_balance
        self.peak_balance = initial_balance
        self.positions: Dict[str, Position] = {}
        self.is_halted = False
        self.halt_reason = None

    def add_position(self, position: Position) -> bool:
        if self.is_halted or position.instrument in self.positions:
            return False
        current_risk = sum(p.risk_amount for p in self.positions.values())
        if current_risk + position.risk_amount > self.peak_balance * (self.config.max_drawdown_pct / 100) * 0.5:
            return False
        self.positions[position.instrument] = position
        return True

    def remove_position(self, instrument: str) -> Optional[Position]:
        return self.positions.pop(instrument, None)

    def update_balance(self, new_balance: float):
        if new_balance > self.peak_balance:
            self.peak_balance = new_balance
        drawdown_pct = (self.peak_balance - new_balance) / self.peak_balance * 100
        if drawdown_pct >= self.config.max_drawdown_pct:
            self.is_halted = True
            self.halt_reason = f"Max drawdown exceeded: {drawdown_pct:.1f}%"
            logger.error(f"CIRCUIT BREAKER: {self.halt_reason}")

    def can_trade(self) -> bool:
        return not self.is_halted

    def get_exposure_report(self, current_balance: float) -> ExposureReport:
        total_risk = sum(p.risk_amount for p in self.positions.values())
        return ExposureReport(
            total_positions=len(self.positions), total_units=sum(p.units for p in self.positions.values()),
            total_risk=total_risk, risk_pct_of_balance=(total_risk / current_balance * 100) if current_balance > 0 else 0,
            max_drawdown_pct=(self.peak_balance - current_balance) / self.peak_balance * 100,
            is_halted=self.is_halted, halt_reason=self.halt_reason)
