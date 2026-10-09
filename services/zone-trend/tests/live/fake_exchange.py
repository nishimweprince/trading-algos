"""An in-memory USDⓈ-M account that behaves like Binance where the OMS cares.

- MARKET orders fill at ``price[symbol]`` and push ``ORDER_TRADE_UPDATE``.
- Algo orders (STOP_MARKET / TAKE_PROFIT_MARKET) rest until ``move(symbol, p)``
  crosses their trigger; then ``ALGO_UPDATE`` (TRIGGERED, with the spawned
  order id) is pushed and the spawned reduce-only market order fills.
- A second closePosition stop in the same direction is rejected (-4130); a stop
  that would trigger at once is rejected (-2021).
- ``fail(method, outcome, code=None, happened=False)`` injects one reply:
  ``unknown`` with ``happened=True`` means the action took effect but the
  caller never heard back.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Reply:
    outcome: str
    body: Any = None
    code: int | None = None
    msg: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


class FakeExchange:
    def __init__(self, prices: dict[str, float]):
        self.price = dict(prices)
        self.position: dict[str, float] = defaultdict(float)
        self.entry: dict[str, float] = defaultdict(float)
        self.orders: dict[str, dict] = {}
        self.algos: dict[str, dict] = {}
        self.trades: dict[str, list[dict]] = defaultdict(list)
        self.calls: list[tuple[str, str]] = []
        self.sink = None  # oms.on_event
        self.stream_up = True
        self._ids = itertools.count(1000)
        self._fail: dict[str, list[tuple]] = defaultdict(list)
        self.now = 1_700_000_000_000

    # -- helpers -------------------------------------------------------------------
    def fail(self, method: str, outcome: str, code: int | None = None, happened: bool = False):
        self._fail[method].append((outcome, code, happened))

    def _injected(self, method: str):
        return self._fail[method].pop(0) if self._fail[method] else None

    def _push(self, ev: dict) -> None:
        if self.sink is not None and self.stream_up:
            self.sink(ev)

    def _execute(
        self, sym: str, side: str, qty: float, cid: str, reduce: bool, oid: int | None = None
    ) -> dict:
        p = self.price[sym]
        sign = 1 if side == "BUY" else -1
        amt = self.position[sym]
        if reduce:
            qty = min(qty, abs(amt))
        new = amt + sign * qty
        realized = 0.0
        if amt and (amt > 0) != (sign > 0):  # reducing
            realized = (p - self.entry[sym]) * min(qty, abs(amt)) * (1 if amt > 0 else -1)
        elif not amt or (amt > 0) == (sign > 0):
            self.entry[sym] = (self.entry[sym] * abs(amt) + p * qty) / (abs(amt) + qty)
        self.position[sym] = new if abs(new) > 1e-12 else 0.0
        oid = oid if oid is not None else next(self._ids)
        tid = next(self._ids)
        fee = qty * p * 0.0005
        order = {
            "orderId": oid,
            "clientOrderId": cid,
            "symbol": sym,
            "status": "FILLED",
            "avgPrice": str(p),
            "executedQty": str(qty),
            "side": side,
        }
        self.orders[cid] = order
        self.trades[sym].append(
            {
                "id": tid,
                "orderId": oid,
                "price": str(p),
                "qty": str(qty),
                "commission": str(fee),
                "commissionAsset": "USDT",
                "realizedPnl": str(realized),
                "maker": False,
                "time": self.now,
                "side": side,
            }
        )
        self._push(
            {
                "e": "ORDER_TRADE_UPDATE",
                "T": self.now,
                "o": {
                    "s": sym,
                    "c": cid,
                    "i": oid,
                    "S": side,
                    "x": "TRADE",
                    "X": "FILLED",
                    "l": str(qty),
                    "L": str(p),
                    "z": str(qty),
                    "ap": str(p),
                    "n": str(fee),
                    "N": "USDT",
                    "t": tid,
                    "rp": str(realized),
                    "R": reduce,
                    "m": False,
                },
            }
        )
        return order

    def move(self, sym: str, p: float) -> None:
        """Set the (mark = last) price and trigger whatever it crosses."""
        self.price[sym] = p
        for cid, a in list(self.algos.items()):
            if a["symbol"] != sym or a["status"] != "NEW":
                continue
            trig = a["triggerPrice"]
            stop = a["type"] == "STOP_MARKET"
            sell = a["side"] == "SELL"
            hit = (p <= trig if sell else p >= trig) if stop else (p >= trig if sell else p <= trig)
            if not hit:
                continue
            a["status"] = "FINISHED"
            child = f"autoclose-{next(self._ids)}"
            qty = abs(self.position[sym]) if a["closePosition"] else a["quantity"]
            if qty <= 0:
                continue
            oid = next(self._ids)  # the order the trigger spawns
            self._push(
                {
                    "e": "ALGO_UPDATE",
                    "o": {"caid": cid, "aid": a["algoId"], "X": "TRIGGERED", "ai": oid},
                }
            )
            self._execute(sym, a["side"], qty, child, reduce=True, oid=oid)

    # -- TradingPort ---------------------------------------------------------------
    async def new_order(
        self, symbol, side, order_type, *, quantity, client_id, reduce_only=False, **_
    ):
        self.calls.append(("new_order", client_id))
        inj = self._injected("new_order")
        if inj and not inj[2]:
            return Reply(inj[0], code=inj[1])
        order = self._execute(symbol, side, float(quantity), client_id, reduce_only)
        if inj:
            return Reply(inj[0], code=inj[1])
        return Reply("ok", dict(order))

    async def query_order(self, symbol, client_id):
        self.calls.append(("query_order", client_id))
        if client_id in self.orders:
            return Reply("ok", dict(self.orders[client_id]))
        return Reply("rejected", code=-2013, msg="Order does not exist.")

    async def new_algo(
        self,
        symbol,
        side,
        order_type,
        *,
        trigger_price,
        client_id,
        close_position=False,
        quantity=None,
        reduce_only=False,
        **_,
    ):
        self.calls.append(("new_algo", client_id))
        inj = self._injected("new_algo")
        if inj and not inj[2]:
            return Reply(inj[0], code=inj[1])
        p, trig = self.price[symbol], float(trigger_price)
        if order_type == "STOP_MARKET" and (
            (side == "SELL" and trig >= p) or (side == "BUY" and trig <= p)
        ):
            return Reply("rejected", code=-2021, msg="Order would immediately trigger.")
        if close_position and any(
            a["closePosition"]
            and a["status"] == "NEW"
            and a["symbol"] == symbol
            and a["side"] == side
            and a["type"] == order_type
            for a in self.algos.values()
        ):
            return Reply("rejected", code=-4130, msg="closePosition order exists")
        algo = {
            "algoId": next(self._ids),
            "clientAlgoId": client_id,
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "triggerPrice": trig,
            "closePosition": close_position,
            "quantity": float(quantity or 0),
            "status": "NEW",
        }
        self.algos[client_id] = algo
        if inj:
            return Reply(inj[0], code=inj[1])
        return Reply("ok", dict(algo))

    async def query_algo(self, client_id):
        a = self.algos.get(client_id)
        return Reply("ok", dict(a)) if a else Reply("rejected", code=-2013)

    async def cancel_algo(self, client_id):
        self.calls.append(("cancel_algo", client_id))
        a = self.algos.get(client_id)
        if a is None or a["status"] != "NEW":
            return Reply("rejected", code=-2011, msg="Unknown order sent.")
        a["status"] = "CANCELED"
        return Reply("ok", dict(a))

    async def open_algo_orders(self, symbol=None):
        return Reply(
            "ok",
            [
                dict(a)
                for a in self.algos.values()
                if a["status"] == "NEW" and (symbol is None or a["symbol"] == symbol)
            ],
        )

    async def positions(self, symbol=None):
        return Reply(
            "ok",
            [
                {"symbol": s, "positionAmt": str(a), "entryPrice": str(self.entry[s])}
                for s, a in self.position.items()
                if a
            ],
        )

    async def user_trades(self, symbol, start_time=None):
        return Reply(
            "ok", [t for t in self.trades[symbol] if start_time is None or t["time"] >= start_time]
        )

    async def income(self, symbol, income_type="FUNDING_FEE", start_time=None):
        return Reply("ok", [])
