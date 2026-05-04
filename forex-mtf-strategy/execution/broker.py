"""
OANDA broker integration for order execution.

Handles order placement, modification, and cancellation through OANDA's v20 API.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from oandapyV20 import API
from oandapyV20.endpoints import accounts, orders, positions, trades
from oandapyV20.exceptions import V20Error

from config.settings import get_settings
from monitoring.logger import get_logger

logger = get_logger(__name__)


class OrderType(Enum):
    """Order type enumeration."""
    
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    MARKET_IF_TOUCHED = "MARKET_IF_TOUCHED"


class OrderSide(Enum):
    """Order side enumeration."""
    
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class OrderRequest:
    """Order request parameters."""
    
    instrument: str
    units: int  # Positive for buy, negative for sell
    order_type: OrderType = OrderType.MARKET
    price: Optional[float] = None  # Required for limit/stop orders
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    trailing_stop_pips: Optional[float] = None
    
    @property
    def side(self) -> OrderSide:
        """Get order side based on units."""
        return OrderSide.BUY if self.units > 0 else OrderSide.SELL


@dataclass
class OrderResult:
    """Order execution result."""
    
    success: bool
    order_id: Optional[str] = None
    trade_id: Optional[str] = None
    fill_price: Optional[float] = None
    units: int = 0
    error_message: Optional[str] = None


class OANDABroker:
    """
    OANDA broker integration for forex trading.
    
    Handles all order management through OANDA's v20 REST API.
    """
    
    def __init__(
        self,
        access_token: Optional[str] = None,
        account_id: Optional[str] = None,
    ):
        """
        Initialize OANDA broker connection.
        
        Args:
            access_token: OANDA API access token
            account_id: OANDA account ID
        """
        settings = get_settings()
        self.access_token = access_token or settings.oanda.access_token
        self.account_id = account_id or settings.oanda.account_id
        self.environment = settings.oanda.environment
        
        if not self.access_token or not self.account_id:
            logger.warning("OANDA credentials not configured. Using paper mode.")
            self._api = None
            self._paper_mode = True
        else:
            self._api = API(access_token=self.access_token, environment=self.environment)
            self._paper_mode = False
    
    @property
    def is_connected(self) -> bool:
        """Check if broker is connected."""
        return self._api is not None
    
    def get_account_summary(self) -> Optional[dict]:
        """
        Get account summary information.
        
        Returns:
            Account summary dict or None if unavailable
        """
        if not self.is_connected:
            return None
        
        try:
            request = accounts.AccountSummary(accountID=self.account_id)
            response = self._api.request(request)
            
            account = response.get("account", {})
            return {
                "balance": float(account.get("balance", 0)),
                "nav": float(account.get("NAV", 0)),
                "unrealized_pnl": float(account.get("unrealizedPL", 0)),
                "margin_used": float(account.get("marginUsed", 0)),
                "margin_available": float(account.get("marginAvailable", 0)),
                "open_trade_count": int(account.get("openTradeCount", 0)),
                "open_position_count": int(account.get("openPositionCount", 0)),
            }
            
        except V20Error as e:
            logger.error(f"Error fetching account summary: {e}")
            return None
    
    def place_market_order(
        self,
        instrument: str,
        units: int,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> OrderResult:
        """
        Place a market order.
        
        Args:
            instrument: Instrument name (e.g., "EUR_USD")
            units: Number of units (positive=buy, negative=sell)
            stop_loss: Stop loss price
            take_profit: Take profit price
            
        Returns:
            OrderResult with execution details
        """
        if self._paper_mode:
            logger.info(f"PAPER: Market order {instrument} {units} units")
            return OrderResult(
                success=True,
                order_id="PAPER_ORDER",
                trade_id="PAPER_TRADE",
                units=units,
            )
        
        order_data = {
            "order": {
                "type": "MARKET",
                "instrument": instrument,
                "units": str(units),
                "timeInForce": "FOK",  # Fill or Kill
                "positionFill": "DEFAULT",
            }
        }
        
        # Add stop loss
        if stop_loss is not None:
            order_data["order"]["stopLossOnFill"] = {
                "price": f"{stop_loss:.5f}",
                "timeInForce": "GTC",
            }
        
        # Add take profit
        if take_profit is not None:
            order_data["order"]["takeProfitOnFill"] = {
                "price": f"{take_profit:.5f}",
                "timeInForce": "GTC",
            }
        
        try:
            request = orders.OrderCreate(self.account_id, data=order_data)
            response = self._api.request(request)
            
            if "orderFillTransaction" in response:
                fill = response["orderFillTransaction"]
                result = OrderResult(
                    success=True,
                    order_id=fill.get("orderID"),
                    trade_id=fill.get("tradeOpened", {}).get("tradeID"),
                    fill_price=float(fill.get("price", 0)),
                    units=int(float(fill.get("units", 0))),
                )
                logger.info(f"Order filled: {instrument} @ {result.fill_price}")
                return result
            else:
                return OrderResult(
                    success=False,
                    error_message="Order not filled",
                )
                
        except V20Error as e:
            logger.error(f"Order error: {e}")
            return OrderResult(success=False, error_message=str(e))
    
    def place_limit_order(
        self,
        instrument: str,
        units: int,
        price: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        gtd_time: Optional[str] = None,
    ) -> OrderResult:
        """
        Place a limit order.
        
        Args:
            instrument: Instrument name
            units: Number of units
            price: Limit price
            stop_loss: Stop loss price
            take_profit: Take profit price
            gtd_time: Good-til-date time (ISO format)
            
        Returns:
            OrderResult with order details
        """
        if self._paper_mode:
            logger.info(f"PAPER: Limit order {instrument} {units} @ {price}")
            return OrderResult(success=True, order_id="PAPER_ORDER", units=units)
        
        order_data = {
            "order": {
                "type": "LIMIT",
                "instrument": instrument,
                "units": str(units),
                "price": f"{price:.5f}",
                "timeInForce": "GTC" if gtd_time is None else "GTD",
                "positionFill": "DEFAULT",
            }
        }
        
        if gtd_time:
            order_data["order"]["gtdTime"] = gtd_time
        
        if stop_loss:
            order_data["order"]["stopLossOnFill"] = {"price": f"{stop_loss:.5f}"}
        
        if take_profit:
            order_data["order"]["takeProfitOnFill"] = {"price": f"{take_profit:.5f}"}
        
        try:
            request = orders.OrderCreate(self.account_id, data=order_data)
            response = self._api.request(request)
            
            order_create = response.get("orderCreateTransaction", {})
            return OrderResult(
                success=True,
                order_id=order_create.get("id"),
                units=units,
            )
            
        except V20Error as e:
            logger.error(f"Limit order error: {e}")
            return OrderResult(success=False, error_message=str(e))
    
    def close_position(
        self,
        instrument: str,
        units: Optional[int] = None,
    ) -> OrderResult:
        """
        Close a position.
        
        Args:
            instrument: Instrument name
            units: Units to close (None = close all)
            
        Returns:
            OrderResult with close details
        """
        if self._paper_mode:
            logger.info(f"PAPER: Close position {instrument}")
            return OrderResult(success=True, order_id="PAPER_CLOSE")
        
        try:
            # Get current position
            request = positions.PositionDetails(
                accountID=self.account_id,
                instrument=instrument,
            )
            response = self._api.request(request)
            position = response.get("position", {})
            
            long_units = int(position.get("long", {}).get("units", 0))
            short_units = int(position.get("short", {}).get("units", 0))
            
            close_data = {}
            if long_units != 0:
                close_data["longUnits"] = "ALL" if units is None else str(abs(units))
            if short_units != 0:
                close_data["shortUnits"] = "ALL" if units is None else str(abs(units))
            
            if not close_data:
                return OrderResult(success=False, error_message="No position to close")
            
            request = positions.PositionClose(
                accountID=self.account_id,
                instrument=instrument,
                data=close_data,
            )
            response = self._api.request(request)
            
            return OrderResult(
                success=True,
                order_id=response.get("relatedTransactionIDs", [""])[0],
            )
            
        except V20Error as e:
            logger.error(f"Close position error: {e}")
            return OrderResult(success=False, error_message=str(e))
    
    def modify_trade(
        self,
        trade_id: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        trailing_stop_distance: Optional[float] = None,
    ) -> bool:
        """
        Modify an existing trade's stop loss or take profit.
        
        Args:
            trade_id: Trade ID to modify
            stop_loss: New stop loss price
            take_profit: New take profit price
            trailing_stop_distance: Trailing stop distance in price units
            
        Returns:
            True if modification successful
        """
        if self._paper_mode:
            logger.info(f"PAPER: Modify trade {trade_id}")
            return True
        
        data = {}
        
        if stop_loss is not None:
            data["stopLoss"] = {"price": f"{stop_loss:.5f}"}
        
        if take_profit is not None:
            data["takeProfit"] = {"price": f"{take_profit:.5f}"}
        
        if trailing_stop_distance is not None:
            data["trailingStopLoss"] = {"distance": f"{trailing_stop_distance:.5f}"}
        
        if not data:
            return True
        
        try:
            request = trades.TradeCRCDO(
                accountID=self.account_id,
                tradeID=trade_id,
                data=data,
            )
            self._api.request(request)
            logger.info(f"Modified trade {trade_id}")
            return True
            
        except V20Error as e:
            logger.error(f"Modify trade error: {e}")
            return False
    
    def close_trade(self, trade_id: str, units: Optional[int] = None) -> OrderResult:
        """
        Close a specific trade.
        
        Args:
            trade_id: Trade ID to close
            units: Units to close (None = close all)
            
        Returns:
            OrderResult with close details
        """
        if self._paper_mode:
            logger.info(f"PAPER: Close trade {trade_id}")
            return OrderResult(success=True, trade_id=trade_id)
        
        data = {}
        if units is not None:
            data["units"] = str(abs(units))
        
        try:
            request = trades.TradeClose(
                accountID=self.account_id,
                tradeID=trade_id,
                data=data if data else None,
            )
            response = self._api.request(request)
            
            return OrderResult(
                success=True,
                trade_id=trade_id,
                fill_price=float(response.get("orderFillTransaction", {}).get("price", 0)),
            )
            
        except V20Error as e:
            logger.error(f"Close trade error: {e}")
            return OrderResult(success=False, error_message=str(e))
    
    def get_open_trades(self, instrument: Optional[str] = None) -> list[dict]:
        """
        Get list of open trades.
        
        Args:
            instrument: Filter by instrument (optional)
            
        Returns:
            List of open trade dicts
        """
        if self._paper_mode:
            return []
        
        try:
            request = trades.OpenTrades(accountID=self.account_id)
            response = self._api.request(request)
            
            all_trades = response.get("trades", [])
            
            if instrument:
                all_trades = [t for t in all_trades if t.get("instrument") == instrument]
            
            return [
                {
                    "trade_id": t.get("id"),
                    "instrument": t.get("instrument"),
                    "units": int(t.get("currentUnits", 0)),
                    "price": float(t.get("price", 0)),
                    "unrealized_pnl": float(t.get("unrealizedPL", 0)),
                    "stop_loss": float(t.get("stopLossOrder", {}).get("price", 0)) or None,
                    "take_profit": float(t.get("takeProfitOrder", {}).get("price", 0)) or None,
                }
                for t in all_trades
            ]
            
        except V20Error as e:
            logger.error(f"Get trades error: {e}")
            return []
    
    def get_pending_orders(self, instrument: Optional[str] = None) -> list[dict]:
        """
        Get list of pending orders.
        
        Args:
            instrument: Filter by instrument (optional)
            
        Returns:
            List of pending order dicts
        """
        if self._paper_mode:
            return []
        
        try:
            request = orders.OrdersPending(accountID=self.account_id)
            response = self._api.request(request)
            
            all_orders = response.get("orders", [])
            
            if instrument:
                all_orders = [o for o in all_orders if o.get("instrument") == instrument]
            
            return [
                {
                    "order_id": o.get("id"),
                    "instrument": o.get("instrument"),
                    "type": o.get("type"),
                    "units": int(o.get("units", 0)),
                    "price": float(o.get("price", 0)) if o.get("price") else None,
                }
                for o in all_orders
            ]
            
        except V20Error as e:
            logger.error(f"Get orders error: {e}")
            return []
    
    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel a pending order.
        
        Args:
            order_id: Order ID to cancel
            
        Returns:
            True if cancellation successful
        """
        if self._paper_mode:
            logger.info(f"PAPER: Cancel order {order_id}")
            return True
        
        try:
            request = orders.OrderCancel(accountID=self.account_id, orderID=order_id)
            self._api.request(request)
            logger.info(f"Cancelled order {order_id}")
            return True
            
        except V20Error as e:
            logger.error(f"Cancel order error: {e}")
            return False
