"""OANDA Broker Integration"""
from dataclasses import dataclass
from typing import Optional, Dict, List
from datetime import datetime
from loguru import logger

try:
    from oandapyV20 import API
    from oandapyV20.endpoints import orders, trades, accounts
    from oandapyV20.exceptions import V20Error
    OANDA_AVAILABLE = True
except ImportError:
    OANDA_AVAILABLE = False

from ..config import OANDAConfig

@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str]
    trade_id: Optional[str]
    fill_price: Optional[float]
    units: Optional[int]
    message: str

@dataclass
class TradeInfo:
    trade_id: str
    instrument: str
    units: int
    direction: int
    open_price: float
    unrealized_pnl: float
    stop_loss: Optional[float]
    take_profit: Optional[float]

class OANDABroker:
    def __init__(self, config: OANDAConfig = None):
        if not OANDA_AVAILABLE:
            raise ImportError("oandapyV20 required")
        self.config = config or OANDAConfig()
        self.api = API(access_token=self.config.api_token, environment=self.config.environment)
        self.account_id = self.config.account_id

    def get_account_info(self) -> Dict:
        request = accounts.AccountDetails(self.account_id)
        response = self.api.request(request)
        acc = response['account']
        return {'balance': float(acc['balance']), 'unrealized_pnl': float(acc['unrealizedPL']),
                'nav': float(acc['NAV']), 'margin_available': float(acc['marginAvailable'])}

    def place_market_order(self, instrument: str, units: int, stop_loss: Optional[float] = None, take_profit: Optional[float] = None) -> OrderResult:
        try:
            order_data = {"type": "MARKET", "instrument": instrument, "units": str(units), "timeInForce": "FOK"}
            if stop_loss: order_data["stopLossOnFill"] = {"price": f"{stop_loss:.5f}"}
            if take_profit: order_data["takeProfitOnFill"] = {"price": f"{take_profit:.5f}"}

            request = orders.OrderCreate(self.account_id, data={"order": order_data})
            response = self.api.request(request)

            if 'orderFillTransaction' in response:
                fill = response['orderFillTransaction']
                return OrderResult(success=True, order_id=fill['id'], trade_id=fill.get('tradeOpened', {}).get('tradeID'),
                                   fill_price=float(fill['price']), units=abs(units), message="Order filled")
            return OrderResult(success=False, order_id=None, trade_id=None, fill_price=None, units=None, message="Order not filled")
        except V20Error as e:
            return OrderResult(success=False, order_id=None, trade_id=None, fill_price=None, units=None, message=str(e))

    def close_trade(self, trade_id: str) -> OrderResult:
        try:
            request = trades.TradeClose(self.account_id, trade_id)
            response = self.api.request(request)
            if 'orderFillTransaction' in response:
                fill = response['orderFillTransaction']
                return OrderResult(success=True, order_id=fill['id'], trade_id=trade_id, fill_price=float(fill['price']),
                                   units=abs(int(float(fill['units']))), message="Trade closed")
            return OrderResult(success=False, order_id=None, trade_id=trade_id, fill_price=None, units=None, message="Close failed")
        except V20Error as e:
            return OrderResult(success=False, order_id=None, trade_id=trade_id, fill_price=None, units=None, message=str(e))

    def get_open_trades(self, instrument: Optional[str] = None) -> List[TradeInfo]:
        params = {'instrument': instrument} if instrument else {}
        request = trades.OpenTrades(self.account_id, params=params)
        response = self.api.request(request)
        return [TradeInfo(trade_id=t['id'], instrument=t['instrument'], units=abs(int(t['currentUnits'])),
                          direction=1 if int(t['currentUnits']) > 0 else -1, open_price=float(t['price']),
                          unrealized_pnl=float(t.get('unrealizedPL', 0)),
                          stop_loss=float(t['stopLossOrder']['price']) if t.get('stopLossOrder') else None,
                          take_profit=float(t['takeProfitOrder']['price']) if t.get('takeProfitOrder') else None)
                for t in response.get('trades', [])]
