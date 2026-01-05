"""Stop Loss Manager"""
from dataclasses import dataclass
from typing import Optional, Tuple
from loguru import logger
from config import RiskConfig

@dataclass
class StopLevels:
    stop_loss: float
    take_profit: float
    breakeven_trigger: float
    trailing_distance: float

class StopManager:
    def __init__(self, config: RiskConfig = None):
        self.config = config or RiskConfig()

    def calculate_stops(self, entry_price: float, atr: float, direction: int) -> StopLevels:
        sl_distance = atr * self.config.stop_loss_atr_mult
        tp_distance = atr * self.config.take_profit_atr_mult
        if direction == 1:
            stop_loss, take_profit = entry_price - sl_distance, entry_price + tp_distance
        else:
            stop_loss, take_profit = entry_price + sl_distance, entry_price - tp_distance
        be_trigger = entry_price * (1 + direction * self.config.breakeven_trigger_pct / 100)
        return StopLevels(stop_loss=stop_loss, take_profit=take_profit, breakeven_trigger=be_trigger, trailing_distance=sl_distance)

    def update_stop(self, current_price: float, entry_price: float, current_stop: float, atr: float, direction: int) -> Tuple[float, bool]:
        profit_pct = ((current_price - entry_price) / entry_price * 100 * direction)
        if profit_pct >= self.config.breakeven_trigger_pct:
            buffer = atr * 0.1
            new_stop = entry_price + (buffer if direction == 1 else -buffer)
            if (direction == 1 and new_stop > current_stop) or (direction == -1 and new_stop < current_stop):
                return new_stop, True
        return current_stop, False
