"""cTrader Open API provider: framing, protocol, handshake, decoding, accounts.

Everything that knows about protobuf, TCP, or cTrader semantics lives here.
Services reach it through the ``ctrader`` entry point in the ``ta.execution``
group (see ta-plugin-api), not by importing it to pick a broker.
"""

from .factory import FACTORY, CTraderFactory

__all__ = ["FACTORY", "CTraderFactory"]
