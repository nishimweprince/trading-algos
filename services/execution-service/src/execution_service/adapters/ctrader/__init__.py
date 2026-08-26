"""The cTrader Open API wire stack: framing, protocol, handshake, decoding.

Everything that knows about protobuf, TCP, or broker semantics lives here. The
modules outside this package deal only in the domain models from `models.py`.
"""
