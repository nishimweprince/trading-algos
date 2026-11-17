"""A stand-in for the execution hub driven by a simulated clock.

It serves M1 candles (UTC interval-end stamps, like the hub), quotes (last close as bid), labelled positions and
deals, fills market orders at the open of the current minute, and runs SL/TP on each new bar with the research's
fill rules (SL first, gap through SL fills at the open).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np

from walkforward_patterns.clock import research_to_utc
from walkforward_patterns.hub import OrderOutcome


@dataclass
class FakePos:
    pid: int
    sym: str
    side: int
    lots: float
    price: float
    sl: float
    tp: float
    label: str
    opened: datetime


@dataclass
class FakeHub:
    data: dict[str, tuple[np.ndarray, ...]]  # sym -> (end_utc_epoch_minutes, o, h, l, c)
    now: datetime = field(default_factory=lambda: datetime(2000, 1, 1, tzinfo=UTC))
    positions_: dict[int, FakePos] = field(default_factory=dict)
    deals_: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    ids: itertools.count = field(default_factory=lambda: itertools.count(1000))
    cursor: dict[str, int] = field(default_factory=dict)
    fail_next_order: str | None = None
    account: str = "ftmo_wf_demo"

    @classmethod
    def from_research(cls, arrays: dict[str, tuple]) -> FakeHub:
        data = {}
        for sym, (t, o, h, l, c) in arrays.items():
            ends = np.array([int(research_to_utc(int(x)).timestamp() // 60) + 1 for x in t], dtype=np.int64)
            data[sym] = (ends, o, h, l, c)
        return cls(data)

    def _now_min(self) -> int:
        return int(self.now.timestamp() // 60)

    def _upto(self, sym: str) -> int:
        ends = self.data[sym][0]
        return int(np.searchsorted(ends, self._now_min(), side="right"))

    def _candle(self, sym: str, i: int) -> dict:
        ends, o, h, l, c = self.data[sym]
        return {"ts": datetime.fromtimestamp(int(ends[i]) * 60, UTC).isoformat(), "open": float(o[i]),
                "high": float(h[i]), "low": float(l[i]), "close": float(c[i]), "volume": 1.0}

    # -------------------------------------------------------------- clock
    def advance(self, now: datetime) -> None:
        """Move the clock; run SL/TP on every bar that closed in between."""
        self.now = now
        for sym in self.data:
            k = self._upto(sym)
            start = self.cursor.get(sym, k)
            for i in range(start, k):
                self._bar(sym, i)
            self.cursor[sym] = k

    def _bar(self, sym: str, i: int) -> None:
        ends, o, h, l, c = self.data[sym]
        for p in list(self.positions_.values()):
            if p.sym != sym or datetime.fromtimestamp((int(ends[i]) - 1) * 60, UTC) < p.opened:
                continue
            px = None
            if p.side == 1:
                if o[i] <= p.sl:
                    px = o[i]
                elif l[i] <= p.sl:
                    px = p.sl
                elif h[i] >= p.tp:
                    px = p.tp
            else:
                if o[i] >= p.sl:
                    px = o[i]
                elif h[i] >= p.sl:
                    px = p.sl
                elif l[i] <= p.tp:
                    px = p.tp
            if px is not None:
                self._close(p, float(px), datetime.fromtimestamp(int(ends[i]) * 60, UTC))

    def _close(self, p: FakePos, px: float, at: datetime) -> None:
        del self.positions_[p.pid]
        units = (100.0 if p.sym == "XAUUSD" else 100_000.0) * p.lots
        gross = p.side * (px - p.price) * units / (px if p.sym.startswith("USD") else 1.0)
        self.deals_.append({"deal_id": next(self.ids), "position_id": p.pid, "closing": True, "price": px,
                            "volume_lots": p.lots, "executed_at": at.isoformat(), "entry_price": p.price,
                            "gross_profit": round(gross, 2), "commission": -0.25, "swap": 0.0})

    # ------------------------------------------------------------- reads
    def accounts(self):
        return [{"alias": self.account, "connected": True, "order_entry_enabled": True, "is_live": False}]

    def quote(self, sym):
        k = self._upto(sym)
        if k == 0:
            return None
        c = float(self.data[sym][4][k - 1])
        return {"bid": c, "ask": c, "spread": 0.0}

    def candles(self, sym, count, to=None):
        k = self._upto(sym)
        if to is not None:
            k = min(k, int(np.searchsorted(self.data[sym][0], int(to.timestamp() // 60), side="right")))
        return [self._candle(sym, i) for i in range(max(0, k - count), k)]

    def history(self, sym, since, page=5000):
        k = self._upto(sym)
        lo = int(np.searchsorted(self.data[sym][0], int(since.timestamp() // 60), side="right"))
        return [self._candle(sym, i) for i in range(lo, k)]

    def positions(self):
        return [{"position_id": p.pid, "instrument": p.sym, "volume_lots": p.lots,
                 "direction": "buy" if p.side == 1 else "sell", "price": p.price, "label": p.label,
                 "opened_at": p.opened.isoformat()} for p in self.positions_.values()]

    def deals(self, since, until=None):
        return [d for d in self.deals_ if datetime.fromisoformat(d["executed_at"]) >= since]

    # ------------------------------------------------------------ writes
    def wait(self, out, seconds=0):
        return out

    def market_order(self, *, source, instrument, side, lots, sl_distance, tp_distance, digits, note=None):
        op = f"op{next(self.ids)}"
        self.orders.append({"source": source, "instrument": instrument, "side": side, "at": self.now})
        if self.fail_next_order:
            state, self.fail_next_order = self.fail_next_order, None
            return OrderOutcome(op, state, error="simulated")
        ends, o, *_ = self.data[instrument]
        i = int(np.searchsorted(ends, self._now_min() + 1))  # the bar of the current minute
        px = float(o[min(i, len(o) - 1)])
        pid = next(self.ids)
        self.positions_[pid] = FakePos(pid, instrument, side, lots, px, px - side * sl_distance,
                                       px + side * tp_distance, source,
                                       self.now.replace(second=0, microsecond=0))
        return OrderOutcome(op, "succeeded", pid, px, lots)

    def close_position(self, *, source, position_id, lots):
        p = self.positions_.get(position_id)
        if p is None:
            return OrderOutcome("x", "rejected", error="POSITION_NOT_FOUND")
        k = self._upto(p.sym)
        self._close(p, float(self.data[p.sym][4][k - 1]), self.now)
        return OrderOutcome("x", "succeeded", position_id)


def minutes(start: datetime, end: datetime):
    t = start
    while t < end:
        yield t
        t += timedelta(minutes=1)
