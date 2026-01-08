import oandapyV20
from oandapyV20 import API
import oandapyV20.endpoints.pricing as pricing
import oandapyV20.endpoints.orders as orders
from ..config import settings
import logging

class OandaBroker:
    def __init__(self):
        self.api = API(access_token=settings.OANDA_ACCESS_TOKEN, 
                       environment=settings.OANDA_ENVIRONMENT)
        self.account_id = settings.OANDA_ACCOUNT_ID
        self.logger = logging.getLogger(__name__)

    def get_price(self, instrument):
        params = {"instruments": instrument}
        r = pricing.PricingInfo(accountID=self.account_id, params=params)
        try:
            rv = self.api.request(r)
            return rv['prices'][0]
        except Exception as e:
            self.logger.error(f"Error fetching price: {e}")
            return None

    def place_market_order(self, instrument, units, stop_loss=None, take_profit=None):
        order_data = {
            "order": {
                "instrument": instrument,
                "units": str(units),
                "type": "MARKET",
                "positionFill": "DEFAULT"
            }
        }
        
        if stop_loss:
            order_data["order"]["stopLossOnFill"] = {"price": str(stop_loss)}
            
        if take_profit:
            order_data["order"]["takeProfitOnFill"] = {"price": str(take_profit)}
            
        r = orders.OrderCreate(self.account_id, data=order_data)
        try:
            rv = self.api.request(r)
            self.logger.info(f"Order placed: {rv}")
            return rv
        except Exception as e:
            self.logger.error(f"Error placing order: {e}")
            return None
