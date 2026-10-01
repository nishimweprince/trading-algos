"""Errors this service raises or catches.

`ServiceError`, the HTTP-shaped failure every service raises, lives in ta-core.
The broker errors live in the plugins that raise them, and
`SymbolResolutionError` in ta-plugin-api because every plugin shares it. All of
them are re-exported here so call sites keep one import.
"""

from __future__ import annotations

from ta_core import ServiceError
from ta_plugin_api import SymbolResolutionError
from ta_plugin_ctrader.errors import CTraderError, CTraderTimeout, FrameError

__all__ = [
    "CTraderError",
    "CTraderTimeout",
    "FrameError",
    "ServiceError",
    "SymbolResolutionError",
]
