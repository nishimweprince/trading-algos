from __future__ import annotations

import backtrader as bt


class SignalStrategy(bt.Strategy):
    params = dict(
        risk_pct=0.01,
        spread_pips=1.3,
        pip_size=0.0001,
        sl_atr_mult=1.5,
        tp_rr=2.0,
        max_units=100_000,
    )

    def __init__(self) -> None:
        self.buy_sig = self.data.buy_signal
        self.sell_sig = self.data.sell_signal
        self.atr = self.data.atr

        self._bracket_orders = []

    def next(self) -> None:
        # Ignore until ATR is ready
        if not self.atr[0]:
            return

        # Don't stack positions
        if self.position:
            return

        if self._has_open_orders():
            return

        spread = (self.p.spread_pips * self.p.pip_size)

        if bool(self.buy_sig[0]):
            entry = float(self.data.close[0] + spread / 2.0)
            sl = entry - float(self.atr[0]) * self.p.sl_atr_mult
            tp = entry + (entry - sl) * self.p.tp_rr
            size = self._risk_size(entry=entry, stop=sl)
            if size > 0:
                self._bracket_orders = self.buy_bracket(
                    size=size,
                    exectype=bt.Order.Market,
                    stopprice=sl,
                    limitprice=tp,
                )
            return

        if bool(self.sell_sig[0]):
            entry = float(self.data.close[0] - spread / 2.0)
            sl = entry + float(self.atr[0]) * self.p.sl_atr_mult
            tp = entry - (sl - entry) * self.p.tp_rr
            size = self._risk_size(entry=entry, stop=sl)
            if size > 0:
                self._bracket_orders = self.sell_bracket(
                    size=size,
                    exectype=bt.Order.Market,
                    stopprice=sl,
                    limitprice=tp,
                )

    def _has_open_orders(self) -> bool:
        return len(self.broker.get_orders_open()) > 0

    def _risk_size(self, *, entry: float, stop: float) -> int:
        cash = float(self.broker.getcash())
        risk_amount = cash * float(self.p.risk_pct)
        sl_distance = abs(entry - stop)
        if sl_distance <= 0:
            return 0
        units = int(risk_amount / sl_distance)
        return max(0, min(units, int(self.p.max_units)))

