"""Live Execution Engine for Capital.com"""
import math
from loguru import logger
from typing import Optional, Dict
import pandas as pd

from data.capital_client import CapitalComClient
from data.capital_feed import CapitalDataFeed
from risk import PositionSizer, StopManager
from config import StrategyConfig
from strategy import Signal, SignalType

class LiveExecutionEngine:
    """
    Handles execution of signals on Capital.com in real-time.
    """
    
    def __init__(self, config: StrategyConfig, feed: CapitalDataFeed):
        self.config = config
        self.client = feed.client
        self.feed = feed
        self.position_sizer = PositionSizer(self.config.risk)
        self.stop_manager = StopManager(self.config.risk)
        
    def execute_signal(self, signal: Signal, instrument: str):
        """
        Execute a trading signal.
        
        Args:
            signal: The signal to execute
            instrument: The instrument name (e.g., 'EUR_USD')
        """
        if signal.type == SignalType.NONE:
            return
            
        logger.info(f"Processing signal: {signal.type.name} for {instrument}")
        
        # 1. Check for existing positions
        existing_positions = self._get_positions_for_instrument(instrument)
        
        if existing_positions:
            logger.info(f"Existing positions found for {instrument}: {len(existing_positions)}")
            
            # If we have a position and get an exit signal or an opposite entry signal, close it
            should_close = False
            for pos in existing_positions:
                # Access nested position data - Capital.com API returns position data nested under 'position' key
                position_data = pos.get('position', {})
                direction_str = position_data.get('direction', '')
                deal_id = position_data.get('dealId', '')
                
                # Skip if position data is missing
                if not direction_str or not deal_id:
                    logger.warning(f"Skipping position with missing data: direction={direction_str}, dealId={deal_id}")
                    continue
                
                pos_direction = 1 if direction_str == 'BUY' else -1
                
                # Exit if signal is an exit signal for this direction
                if (pos_direction == 1 and signal.type == SignalType.EXIT_LONG) or \
                   (pos_direction == -1 and signal.type == SignalType.EXIT_SHORT):
                    should_close = True
                
                # Exit if signal is an entry in the opposite direction (flip)
                elif (pos_direction == 1 and signal.type == SignalType.SHORT) or \
                     (pos_direction == -1 and signal.type == SignalType.LONG):
                    should_close = True
                    
                if should_close:
                    logger.info(f"Closing position {deal_id} due to signal {signal.type.name}")
                    try:
                        self.client.close_position(deal_id)
                        logger.info(f"Successfully closed position {deal_id}")
                    except Exception as e:
                        logger.error(f"Failed to close position {deal_id}: {e}")
                        
        # 2. If it's an entry signal and we don't have a position, enter
        if signal.type in [SignalType.LONG, SignalType.SHORT]:
            # Re-check positions after potential closure
            current_positions = self._get_positions_for_instrument(instrument)
            if not current_positions:
                self._enter_position(signal, instrument)
            else:
                logger.info(f"Skipping entry: already have position(s) in {instrument}")

    def _get_positions_for_instrument(self, instrument: str) -> list:
        """Get all open positions for a specific instrument."""
        try:
            epic = self.feed._instrument_to_epic(instrument)
            all_positions = self.client.get_positions().get('positions', [])
            
            # Filter positions by epic
            instrument_positions = [p for p in all_positions if p.get('market', {}).get('epic') == epic]
            return instrument_positions
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            return []

    def _enter_position(self, signal: Signal, instrument: str):
        """Execute a new entry."""
        try:
            # 1. Fetch current account equity
            equity = self.feed.get_equity()
            if equity <= 0:
                logger.error("Cannot enter position: account equity is 0 or unknown")
                return
                
            logger.info(f"Current account equity: {equity:.2f}")
            
            # 2. Calculate position size
            # Ensure we have stop loss for sizing
            if not signal.stop_loss:
                logger.warning("Signal missing stop loss, cannot calculate position size precisely")
                # Fallback or use ATR? 
                # For now, let's assume signal has SL if it was generated correctly
                return
                
            pos_size = self.position_sizer.calculate_position_size(
                balance=equity,
                entry_price=signal.price,
                stop_loss=signal.stop_loss,
                instrument=instrument
            )
            
            if pos_size.units <= 0:
                logger.warning(f"Calculated position size is 0 for {instrument}")
                return
                
            logger.info(f"Calculated position size: {pos_size.units} units (Risk: {pos_size.risk_amount:.2f}, {pos_size.risk_pct}%)")
            
            # 3. Place the order
            direction = 'BUY' if signal.type == SignalType.LONG else 'SELL'
            epic = self.feed._instrument_to_epic(instrument)
            order_size = self._to_capital_order_size(instrument, pos_size.units)

            # Fetch market details to get the instrument's price precision
            market_info = self.client.get_market_details(epic)
            decimals = self._get_decimal_places(market_info)

            stop_loss = self._round_stop_loss(signal.stop_loss, decimals, direction)
            take_profit = self._round_take_profit(signal.take_profit, decimals, direction)

            logger.info(
                f"Placing {direction} order for {instrument} ({epic}): "
                f"{order_size} size (from {pos_size.units} units), "
                f"SL={stop_loss}, TP={take_profit} (decimals={decimals})"
            )

            response = self.client.create_position(
                epic=epic,
                direction=direction,
                size=order_size,
                stop_loss=stop_loss,
                take_profit=take_profit
            )
            
            deal_reference = response.get('dealReference')
            logger.info(f"Order placed successfully! Deal Reference: {deal_reference}")
            
        except Exception as e:
            logger.error(f"Failed to enter position for {instrument}: {e}")
            import traceback
            logger.error(traceback.format_exc())

    @staticmethod
    def _to_capital_order_size(instrument: str, units: int) -> float:
        """
        Convert internal "units" to Capital.com order size.

        Internal sizing is in OANDA-like units (100,000 units = 1.0 FX lot).
        Capital.com order size expects lot/contract size, not raw units.
        """
        # Detect standard FX pair format like EUR_USD / USD_JPY.
        is_fx_pair = False
        if "_" in instrument:
            parts = instrument.split("_")
            is_fx_pair = len(parts) == 2 and all(len(part) == 3 and part.isalpha() for part in parts)

        if is_fx_pair:
            # Convert units to lots and enforce a practical minimum lot size.
            lots = units / 100000.0
            return max(round(lots, 4), 0.01)

        # For non-FX instruments (indices/commodities/crypto), keep units as size.
        return float(max(units, 1))

    @staticmethod
    def _get_decimal_places(market_info: Dict) -> int:
        """Extract the number of decimal places from Capital.com market details."""
        snapshot = market_info.get('snapshot', {})
        scaling_factor = snapshot.get('scalingFactor', 1)
        if scaling_factor and scaling_factor > 0:
            return max(0, round(math.log10(scaling_factor)))
        return 2

    @staticmethod
    def _round_stop_loss(price: float, decimals: int, direction: str) -> float:
        """Round stop loss away from entry to satisfy minimum distance."""
        factor = 10 ** decimals
        if direction == 'BUY':
            # SL is below entry — round down to ensure it's far enough
            return math.floor(price * factor) / factor
        # SL is above entry — round up to ensure it's far enough
        return math.ceil(price * factor) / factor

    @staticmethod
    def _round_take_profit(price: float, decimals: int, direction: str) -> float:
        """Round take profit away from entry to satisfy minimum distance."""
        factor = 10 ** decimals
        if direction == 'BUY':
            # TP is above entry — round up
            return math.ceil(price * factor) / factor
        # TP is below entry — round down
        return math.floor(price * factor) / factor
