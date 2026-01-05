"""Position Sizing Module"""
from dataclasses import dataclass
from loguru import logger
from ..config import RiskConfig

@dataclass
class PositionSize:
    units: int
    risk_amount: float
    risk_pct: float
    pip_value: float
    pip_distance: float

class PositionSizer:
    PIP_VALUES = {'EUR_USD': 10.0, 'GBP_USD': 10.0, 'AUD_USD': 10.0, 'USD_CHF': 10.0, 'USD_CAD': 10.0, 'USD_JPY': 9.0}

    def __init__(self, config: RiskConfig = None):
        self.config = config or RiskConfig()

    def calculate_position_size(self, balance: float, entry_price: float, stop_loss: float, instrument: str = 'EUR_USD') -> PositionSize:
        risk_amount = balance * (self.config.risk_per_trade_pct / 100)
        pip_distance = abs(entry_price - stop_loss)
        pip_distance_pips = pip_distance * (100 if 'JPY' in instrument else 10000)

        if pip_distance_pips == 0:
            return PositionSize(units=0, risk_amount=0, risk_pct=0, pip_value=0, pip_distance=0)

        pip_value = self.PIP_VALUES.get(instrument, 10.0)
        units = int((risk_amount / pip_distance_pips / pip_value) * 100000)
        max_units = int(balance * (self.config.max_position_pct / 100) / entry_price * 100000)
        units = max(min(units, max_units), 1000)

        return PositionSize(units=units, risk_amount=risk_amount, risk_pct=self.config.risk_per_trade_pct, pip_value=pip_value, pip_distance=pip_distance_pips)

    def validate_trade(self, entry_price: float, stop_loss: float, take_profit: float) -> bool:
        risk, reward = abs(entry_price - stop_loss), abs(take_profit - entry_price)
        return (reward / risk if risk > 0 else 0) >= self.config.min_risk_reward
