from __future__ import annotations

import backtrader as bt


class SignalPandasData(bt.feeds.PandasData):
    """
    PandasData feed with additional precomputed columns.
    """

    lines = ("buy_signal", "sell_signal", "atr")

    params = (
        ("datetime", None),
        ("open", "open"),
        ("high", "high"),
        ("low", "low"),
        ("close", "close"),
        ("volume", "volume"),
        ("openinterest", None),
        ("buy_signal", "buy_signal"),
        ("sell_signal", "sell_signal"),
        ("atr", "atr"),
    )

