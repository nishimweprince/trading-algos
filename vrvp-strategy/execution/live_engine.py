"""Live Execution Engine for Capital.com"""
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
            
            logger.info(f"Placing {direction} order for {instrument} ({epic}): {pos_size.units} units")
            
            # Optional: Capital.com might require integer size or specific increments
            # The PositionSizer returns units as int, but we might need to check with market details
            
            response = self.client.create_position(
                epic=epic,
                direction=direction,
                size=float(pos_size.units),
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit
            )
            
            deal_reference = response.get('dealReference')
            logger.info(f"Order placed successfully! Deal Reference: {deal_reference}")
            
        except Exception as e:
            logger.error(f"Failed to enter position for {instrument}: {e}")
            import traceback
            logger.error(traceback.format_exc())
