"""Position Sizing Module - universal contracts-based formula."""
from dataclasses import dataclass
from loguru import logger
from config import RiskConfig
from data.instrument_specs import get_instrument_spec

@dataclass
class PositionSize:
    contracts: float
    risk_amount: float
    risk_pct: float
    value_per_point: float
    price_distance: float

class PositionSizer:
    def __init__(self, config: RiskConfig = None):
        self.config = config or RiskConfig()

    def calculate_position_size(self, balance: float, entry_price: float, stop_loss: float, instrument: str = 'EUR_USD') -> PositionSize:
        spec = get_instrument_spec(instrument)
        risk_amount = balance * (self.config.risk_per_trade_pct / 100)
        price_distance = abs(entry_price - stop_loss)

        if price_distance == 0:
            return PositionSize(contracts=0.0, risk_amount=0, risk_pct=0, value_per_point=0, price_distance=0)

        risk_per_contract = price_distance * spec.value_per_point
        contracts = risk_amount / risk_per_contract

        # Enforce max position
        max_value = balance * (self.config.max_position_pct / 100)
        if entry_price > 0 and spec.value_per_point > 0:
            max_contracts = max_value / (entry_price * spec.value_per_point)
            contracts = min(contracts, max_contracts)

        contracts = max(contracts, spec.min_size)

        return PositionSize(contracts=round(contracts, 4), risk_amount=risk_amount,
                            risk_pct=self.config.risk_per_trade_pct,
                            value_per_point=spec.value_per_point, price_distance=price_distance)

    def validate_trade(self, entry_price: float, stop_loss: float, take_profit: float) -> bool:
        risk, reward = abs(entry_price - stop_loss), abs(take_profit - entry_price)
        return (reward / risk if risk > 0 else 0) >= self.config.min_risk_reward
