"""DTO Transformers - Convert API-specific responses to normalized DTOs"""
from abc import ABC, abstractmethod
from typing import List
from datetime import datetime
import pandas as pd
from loguru import logger

from .dto import CandleDTO, PriceDTO, AccountDTO, OrderDTO, TradeDTO


class BaseDTOTransformer(ABC):
    """Abstract base class for all DTO transformers"""
    
    @abstractmethod
    def transform_candles(self, raw_response: dict) -> List[CandleDTO]:
        """Transform raw API candle response to List[CandleDTO]"""
        pass
    
    @abstractmethod
    def transform_price(self, raw_response: dict) -> PriceDTO:
        """Transform raw API price response to PriceDTO"""
        pass
    
    @abstractmethod
    def transform_account(self, raw_response: dict) -> AccountDTO:
        """Transform raw API account response to AccountDTO"""
        pass
    
    @abstractmethod
    def transform_order(self, raw_response: dict) -> OrderDTO:
        """Transform raw API order response to OrderDTO"""
        pass
    
    @abstractmethod
    def transform_trade(self, raw_response: dict) -> TradeDTO:
        """Transform raw API trade/position response to TradeDTO"""
        pass
    
    @abstractmethod
    def transform_trades_list(self, raw_response: dict) -> List[TradeDTO]:
        """Transform raw API trades/positions list response to List[TradeDTO]"""
        pass


class CapitalComDTOTransformer(BaseDTOTransformer):
    """Transforms Capital.com API responses to normalized DTOs"""
    
    def transform_candles(self, raw_response: dict) -> List[CandleDTO]:
        """Transform Capital.com candle response to List[CandleDTO]"""
        candles = []
        try:
            prices = raw_response.get('prices', [])
            for item in prices:
                # Capital.com API structure may vary - adjust based on actual API response
                timestamp_str = item.get('snapshotTimeUTC') or item.get('snapshotTime')
                timestamp = pd.Timestamp(timestamp_str) if timestamp_str else pd.Timestamp.now()
                
                # Handle nested price structure
                open_price = item.get('openPrice', {})
                high_price = item.get('highPrice', {})
                low_price = item.get('lowPrice', {})
                close_price = item.get('closePrice', {})
                
                # Use bid price for OHLC (or mid if available)
                open_val = float(open_price.get('bid', open_price.get('mid', open_price.get('openPrice', 0))))
                high_val = float(high_price.get('bid', high_price.get('mid', high_price.get('highPrice', 0))))
                low_val = float(low_price.get('bid', low_price.get('mid', low_price.get('lowPrice', 0))))
                close_val = float(close_price.get('bid', close_price.get('mid', close_price.get('closePrice', 0))))
                
                volume = int(item.get('lastTradedVolume', item.get('volume', 0)))
                
                candles.append(CandleDTO(
                    timestamp=timestamp,
                    open=open_val,
                    high=high_val,
                    low=low_val,
                    close=close_val,
                    volume=volume
                ))
        except Exception as e:
            logger.error(f"Error transforming candles: {e}")
            raise
        return candles
    
    def transform_price(self, raw_response: dict) -> PriceDTO:
        """Transform Capital.com price response to PriceDTO"""
        try:
            # Capital.com price structure
            bid = float(raw_response.get('bid', raw_response.get('bidPrice', 0)))
            ask = float(raw_response.get('ask', raw_response.get('askPrice', 0)))
            mid = (bid + ask) / 2 if bid and ask else float(raw_response.get('mid', 0))
            spread = ask - bid if bid and ask else float(raw_response.get('spread', 0))
            
            timestamp_str = raw_response.get('timestamp', raw_response.get('snapshotTimeUTC'))
            timestamp = pd.Timestamp(timestamp_str) if timestamp_str else pd.Timestamp.now()
            
            return PriceDTO(
                bid=bid,
                ask=ask,
                mid=mid,
                spread=spread,
                timestamp=timestamp
            )
        except Exception as e:
            logger.error(f"Error transforming price: {e}")
            raise
    
    def transform_account(self, raw_response: dict) -> AccountDTO:
        """Transform Capital.com account response to AccountDTO"""
        try:
            account = raw_response.get('accounts', [{}])[0] if isinstance(raw_response.get('accounts'), list) else raw_response
            
            balance = float(account.get('balance', account.get('available', 0)))
            equity = float(account.get('equity', account.get('balance', 0)))
            margin_available = float(account.get('available', account.get('marginAvailable', 0)))
            margin_used = float(account.get('used', account.get('marginUsed', 0)))
            unrealized_pnl = float(account.get('unrealizedProfitLoss', account.get('unrealizedPL', 0)))
            
            return AccountDTO(
                balance=balance,
                equity=equity,
                margin_available=margin_available,
                margin_used=margin_used,
                unrealized_pnl=unrealized_pnl
            )
        except Exception as e:
            logger.error(f"Error transforming account: {e}")
            raise
    
    def transform_order(self, raw_response: dict) -> OrderDTO:
        """Transform Capital.com order response to OrderDTO"""
        try:
            deal_reference = raw_response.get('dealReference', raw_response.get('dealId', ''))
            epic = raw_response.get('epic', raw_response.get('instrument', ''))
            direction_str = raw_response.get('direction', '').upper()
            direction = 1 if direction_str == 'BUY' else -1
            
            size = int(float(raw_response.get('size', raw_response.get('quantity', 0))))
            price = float(raw_response.get('level', raw_response.get('price', raw_response.get('openLevel', 0))))
            status = raw_response.get('status', raw_response.get('dealStatus', 'UNKNOWN'))
            
            return OrderDTO(
                order_id=str(deal_reference),
                instrument=epic,
                direction=direction,
                units=size,
                price=price,
                status=status
            )
        except Exception as e:
            logger.error(f"Error transforming order: {e}")
            raise
    
    def transform_trade(self, raw_response: dict) -> TradeDTO:
        """Transform Capital.com trade/position response to TradeDTO"""
        try:
            deal_id = str(raw_response.get('dealId', raw_response.get('dealReference', '')))
            epic = raw_response.get('epic', raw_response.get('instrument', ''))
            direction_str = raw_response.get('direction', '').upper()
            direction = 1 if direction_str == 'BUY' else -1
            
            size = int(float(raw_response.get('size', raw_response.get('quantity', 0))))
            entry_price = float(raw_response.get('openLevel', raw_response.get('entryLevel', raw_response.get('price', 0))))
            current_price = float(raw_response.get('level', raw_response.get('currentPrice', entry_price)))
            unrealized_pnl = float(raw_response.get('unrealizedProfitLoss', raw_response.get('unrealizedPL', 0)))
            
            stop_loss = None
            if raw_response.get('stopLevel'):
                stop_loss = float(raw_response['stopLevel'])
            elif raw_response.get('stopLoss'):
                stop_loss = float(raw_response['stopLoss'])
            
            take_profit = None
            if raw_response.get('profitLevel'):
                take_profit = float(raw_response['profitLevel'])
            elif raw_response.get('takeProfit'):
                take_profit = float(raw_response['takeProfit'])
            
            return TradeDTO(
                trade_id=deal_id,
                instrument=epic,
                direction=direction,
                units=size,
                entry_price=entry_price,
                current_price=current_price,
                unrealized_pnl=unrealized_pnl,
                stop_loss=stop_loss,
                take_profit=take_profit
            )
        except Exception as e:
            logger.error(f"Error transforming trade: {e}")
            raise
    
    def transform_trades_list(self, raw_response: dict) -> List[TradeDTO]:
        """Transform Capital.com positions list response to List[TradeDTO]"""
        trades = []
        try:
            positions = raw_response.get('positions', [])
            if not positions and isinstance(raw_response, list):
                positions = raw_response
            
            for position in positions:
                trades.append(self.transform_trade(position))
        except Exception as e:
            logger.error(f"Error transforming trades list: {e}")
            raise
        return trades



