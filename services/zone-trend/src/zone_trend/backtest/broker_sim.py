"""Simulated Binance USDⓈ-M broker, stepped one hour at a time on 1m data.

Within each minute, in this order:

1. Funding settlements at that minute: ``cash -= side * notional * rate``
   (longs pay positive rates). Settlement precedes an entry at the same
   instant, as a live order placed seconds after the hour would.
2. On the hour's first minute, pending next-open exits and entries fill at the
   open, plus or minus the symbol's slippage. Entries that open beyond their
   stop or TP1 are skipped.
3. Stop: triggered when the mark minute (last-price minute where mark data is
   missing) trades through it; fills at the stop, or the open if the minute
   gapped past it, with twice the base slippage.
4. Take-profits trigger on last-price minutes and fill at the trigger, taker
   fee (TAKE_PROFIT_MARKET). TP1 closes ``tp1_qty`` and moves the stop to
   entry plus round-trip fees at once (the live OMS does the same on the TP1
   fill). TP2 closes the rest.

A stop and a take-profit in the same minute resolve as the stop (the
pessimistic reading); the run counts these conflicts. A mark price reaching
the liquidation price with the position still open raises, because the stop
rule is meant to make that impossible.

Fees: taker on every fill (all orders here are market or *_MARKET).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from zone_trend.backtest.market import Funding, MinuteBars, MinuteSource
from zone_trend.risk.liquidation import liquidation_price
from zone_trend.risk.sizing import Size
from zone_trend.types import Position, Side, Signal

__all__ = ["Broker", "LiquidationError", "PendingEntry", "SimPosition"]

HOUR = 3_600_000


class LiquidationError(RuntimeError):
    pass


@dataclass
class PendingEntry:
    signal: Signal
    size: Size
    slip: float  # base slippage as a price fraction
    k_sym: float
    size_mult: float
    managed: bool


@dataclass
class SimPosition:
    pos: Position
    entry_type: str
    slip: float
    quality: int
    signal_time: int
    managed: bool
    tp1_qty: float
    single_tp: bool
    liq: float
    k_sym: float
    size_mult: float
    fees: float = 0.0
    funding: float = 0.0
    gross: float = 0.0
    exit_reasons: list[str] = field(default_factory=list)
    pending_exit: str | None = None
    mfe_time: int = 0

    @property
    def notional_open(self) -> float:
        return self.pos.qty_open * self.pos.entry


class Broker:
    def __init__(self, cash: float, fee_taker: float, leverage: float):
        self.cash = cash
        self.fee = fee_taker
        self.leverage = leverage
        self.pending: dict[str, PendingEntry] = {}
        self.positions: dict[str, SimPosition] = {}
        self.fills: list[dict] = []
        self.closed: list[dict] = []
        self.skipped: list[tuple[Signal, str]] = []
        self.conflicts = 0

    # -- orders --------------------------------------------------------------------
    def submit(self, entry: PendingEntry) -> None:
        self.pending[entry.signal.symbol] = entry

    def request_exit(self, symbol: str, reason: str) -> None:
        sp = self.positions.get(symbol)
        if sp is not None and sp.pending_exit is None:
            sp.pending_exit = reason

    # -- accounting ----------------------------------------------------------------
    def equity(self, marks: dict[str, float]) -> float:
        unreal = 0.0
        for sym, sp in self.positions.items():
            p = sp.pos
            unreal += p.side.sign * p.qty_open * (marks.get(sym, p.entry) - p.entry)
        return self.cash + unreal

    def margin(self) -> float:
        return sum(sp.notional_open for sp in self.positions.values()) / self.leverage

    def _fill(self, sp: SimPosition, t: int, qty: float, price: float, reason: str) -> None:
        p = sp.pos
        fee = qty * price * self.fee
        pnl = p.side.sign * qty * (price - p.entry)
        self.cash += pnl - fee
        sp.fees += fee
        sp.gross += pnl
        p.qty_open = max(p.qty_open - qty, 0.0)
        sp.exit_reasons.append(reason)
        self.fills.append(
            {
                "symbol": p.symbol,
                "signal_id": p.signal_id,
                "time": t,
                "leg": reason,
                "side": "sell" if p.side is Side.LONG else "buy",
                "qty": qty,
                "price": price,
                "fee": fee,
            }
        )
        if p.qty_open <= 1e-12:
            self._close(sp, t)

    def _close(self, sp: SimPosition, t: int) -> None:
        p = sp.pos
        risk = p.qty * p.r
        net = sp.gross - sp.fees - sp.funding
        self.closed.append(
            {
                "symbol": p.symbol,
                "side": p.side.value,
                "entry_type": sp.entry_type,
                "signal_id": p.signal_id,
                "zone_id": p.zone_id,
                "quality": sp.quality,
                "signal_time": sp.signal_time,
                "entry_time": p.opened_at,
                "exit_time": t,
                "entry": p.entry,
                "stop_initial": p.stop_initial,
                "stop_pct": p.r / p.entry,
                "tp1": p.tp1,
                "tp2": p.tp2,
                "qty": p.qty,
                "notional": p.qty * p.entry,
                "single_tp": sp.single_tp,
                "k_sym": sp.k_sym,
                "size_mult": sp.size_mult,
                "exit_reason": sp.exit_reasons[-1],
                "exit_legs": ",".join(sp.exit_reasons),
                "gross": sp.gross,
                "fees": sp.fees,
                "funding": sp.funding,
                "net": net,
                "risk_usd": risk,
                "r_mult": net / risk if risk > 0 else math.nan,
                "mfe_r": p.mfe_r,
                "mae_r": p.mae_r,
                "mfe_pct": p.side.sign * (p.best - p.entry) / p.entry,
                "mae_pct": p.side.sign * (p.entry - p.worst) / p.entry,
                "bars_open": p.bars_open,
                "bars_to_mfe": (sp.mfe_time - p.opened_at) / HOUR if sp.mfe_time else 0.0,
            }
        )
        del self.positions[p.symbol]

    # -- the hour ------------------------------------------------------------------
    def process_hour(self, hour_start: int, minutes: MinuteSource, funding: Funding) -> None:
        symbols = sorted(set(self.pending) | set(self.positions))
        for sym in symbols:
            m = minutes.hour(sym, hour_start)
            if len(m) == 0:
                if sym in self.pending:
                    self.skipped.append((self.pending.pop(sym).signal, "no_minute_data"))
                continue
            settles = funding.settlements(sym, hour_start, hour_start + HOUR)
            self._symbol_hour(sym, m, settles)

    def _symbol_hour(self, sym: str, m: MinuteBars, settles: list[tuple[int, float]]) -> None:
        sp = self.positions.get(sym)
        if not settles and sym not in self.pending and sp is not None and sp.pending_exit is None:
            if self._quiet_hour(sp, m):
                return
        si = 0
        for k in range(len(m)):
            ts = int(m.open_time[k])
            while si < len(settles) and settles[si][0] <= ts:
                sp = self.positions.get(sym)
                if sp is not None:
                    p = sp.pos
                    amount = p.side.sign * p.qty_open * m.mark_close[k] * settles[si][1]
                    sp.funding += amount
                    self.cash -= amount
                si += 1
            if k == 0:
                self._at_open(sym, ts, float(m.open[0]))
            sp = self.positions.get(sym)
            if sp is not None:
                self._minute(sp, ts, m, k)

    def _quiet_hour(self, sp: SimPosition, m: MinuteBars) -> bool:
        """Fast path: nothing can trigger this hour; just update excursions."""
        p = sp.pos
        tps = [x for x in (p.tp1 if not p.tp1_done else None, p.tp2) if x and not math.isnan(x)]
        if p.side is Side.LONG:
            if m.mark_low.min() <= p.stop or (tps and m.high.max() >= min(tps)):
                return False
            hi, lo = m.high.max(), m.low.min()
            if hi > p.best:
                p.best = float(hi)
                sp.mfe_time = int(m.open_time[int(np.argmax(m.high))])
            p.worst = min(p.worst, float(lo))
        else:
            if m.mark_high.max() >= p.stop or (tps and m.low.min() <= max(tps)):
                return False
            hi, lo = m.high.max(), m.low.min()
            if lo < p.best:
                p.best = float(lo)
                sp.mfe_time = int(m.open_time[int(np.argmin(m.low))])
            p.worst = max(p.worst, float(hi))
        self._check_liq(sp, m.mark_low.min(), m.mark_high.max())
        return True

    def _at_open(self, sym: str, ts: int, open_: float) -> None:
        sp = self.positions.get(sym)
        if sp is not None and sp.pending_exit is not None:
            price = open_ * (1 - sp.pos.side.sign * sp.slip)
            self._fill(sp, ts, sp.pos.qty_open, price, sp.pending_exit)
        pe = self.pending.pop(sym, None)
        if pe is None or sym in self.positions:
            if pe is not None:
                self.skipped.append((pe.signal, "position_open"))
            return
        sig = pe.signal
        side = sig.side
        price = open_ * (1 + side.sign * pe.slip)
        if side.sign * (price - sig.stop) <= 0:
            self.skipped.append((sig, "gap_through_stop"))
            return
        if not math.isnan(sig.tp1) and side.sign * (price - sig.tp1) >= 0:
            self.skipped.append((sig, "gap_through_tp"))
            return
        qty = pe.size.qty
        fee = qty * price * self.fee
        self.cash -= fee
        pos = Position(
            sig.symbol,
            side,
            sig.id,
            price,
            sig.stop,
            sig.stop,
            sig.tp1,
            sig.tp2,
            qty,
            qty,
            ts,
            zone_id=sig.zone_id,
        )
        sp = SimPosition(
            pos,
            sig.entry_type,
            pe.slip,
            sig.quality,
            sig.bar_time,
            pe.managed,
            pe.size.tp1_qty,
            pe.size.single_tp,
            liquidation_price(price, qty, side.sign, self.leverage),
            pe.k_sym,
            pe.size_mult,
            fees=fee,
        )
        self.positions[sym] = sp
        self.fills.append(
            {
                "symbol": sym,
                "signal_id": sig.id,
                "time": ts,
                "leg": "entry",
                "side": "buy" if side is Side.LONG else "sell",
                "qty": qty,
                "price": price,
                "fee": fee,
            }
        )

    def _minute(self, sp: SimPosition, ts: int, m: MinuteBars, k: int) -> None:
        p = sp.pos
        long = p.side is Side.LONG
        hi, lo, op = float(m.high[k]), float(m.low[k]), float(m.open[k])
        mark_hi, mark_lo = float(m.mark_high[k]), float(m.mark_low[k])
        if (long and hi > p.best) or (not long and lo < p.best):
            p.best = hi if long else lo
            sp.mfe_time = ts
        p.worst = min(p.worst, lo) if long else max(p.worst, hi)

        stop_hit = mark_lo <= p.stop if long else mark_hi >= p.stop
        tp1 = None if (p.tp1_done or math.isnan(p.tp1)) else p.tp1
        tp_touch = any(x is not None and (hi >= x if long else lo <= x) for x in (tp1, p.tp2))
        if stop_hit:
            if tp_touch:
                self.conflicts += 1
            ref = min(p.stop, op) if long else max(p.stop, op)
            price = ref * (1 - p.side.sign * 2 * sp.slip)
            self._fill(sp, ts, p.qty_open, price, "stop" if not p.tp1_done else "stop_after_tp1")
            return
        if tp1 is not None and (hi >= tp1 if long else lo <= tp1):
            qty = p.qty_open if sp.single_tp else min(sp.tp1_qty, p.qty_open)
            p.tp1_done = True
            self._fill(sp, ts, qty, tp1, "tp1")
            if p.symbol not in self.positions:
                return
            p.stop = p.entry * (1 + p.side.sign * 2 * self.fee)
        if p.tp2 is not None and (hi >= p.tp2 if long else lo <= p.tp2):
            self._fill(sp, ts, p.qty_open, p.tp2, "tp2")
            return
        self._check_liq(sp, mark_lo, mark_hi)

    @staticmethod
    def _check_liq(sp: SimPosition, mark_lo: float, mark_hi: float) -> None:
        p = sp.pos
        hit = mark_lo <= sp.liq if p.side is Side.LONG else mark_hi >= sp.liq
        if hit and p.qty_open > 0:
            raise LiquidationError(f"{p.symbol} {p.side.value} reached liquidation {sp.liq}")
