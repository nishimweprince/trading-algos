"""MetaTrader 5 provider: terminal wrapper, symbol manifest, test double.

Services reach it through the ``mt5`` entry point in the ``ta.execution`` group
(see ta-plugin-api). Importing this package never imports MetaTrader5; only
``FACTORY.terminal()`` does.
"""

from .factory import FACTORY, MT5Factory

__all__ = ["FACTORY", "MT5Factory"]
