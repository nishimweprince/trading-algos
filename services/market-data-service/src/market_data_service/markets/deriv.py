"""Deriv synthetics (Volatility, Crash/Boom, Step).

MT5 is the usual source because the strategies trading these execute on the
same Deriv MT5 terminal; a Deriv cTrader account is the alternative. There is
deliberately no native Deriv WebSocket provider: data would come from a
different feed than the one that fills the orders.
"""

ALLOWED_PROVIDERS = frozenset({"mt5", "ctrader"})
