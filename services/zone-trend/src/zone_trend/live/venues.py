"""The two places the runner can send decisions: paper (shadow) and Binance (live).

Both present the same small surface to the runner, so the hourly cycle is one
code path whatever the mode.

``PaperVenue`` is the backtest's broker simulator fed with real data: before
each hourly decision it fetches the hour's 1m last and mark klines and the
funding settlements for every symbol with a position or pending entry, and
replays them through ``Broker`` exactly as a backtest would. Shadow fills are
therefore the backtest's fill model on live prices; nothing is ever sent.

``LiveVenue`` is the OMS plus the database. Its equity counts only zone-trend
positions (``ZT_CAPITAL`` plus realized net plus open PnL), so it works the
same on a dedicated sub-account or a shared one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from zone_trend.backtest.broker_sim import Broker, PendingEntry
from zone_trend.backtest.market import MinuteBars
from zone_trend.live.oms import Oms, TradingPort
from zone_trend.persistence.repo import Repo
from zone_trend.risk.decide import Decision
from zone_trend.types import Position, Side, Signal

__all__ = ["LiveVenue", "PaperVenue", "Venue"]

HOUR = 3_600_000
MINUTE = 60_000


class Venue(Protocol):
    mode: str

    async def before_hour(self, hour_start: int) -> None: ...
    def positions(self) -> dict[str, Position]: ...
    def pending(self) -> dict[str, tuple[Side, float]]: ...
    def equity(self, marks: dict[str, float]) -> float: ...
    def margin(self) -> float: ...
    async def open(self, sig: Signal, d: Decision, k_sym: float) -> None: ...
    async def move_stop(self, symbol: str, price: float) -> None: ...
    async def exit(self, symbol: str, reason: str) -> None: ...
    def closed_trades(self) -> list[dict]: ...


# -- paper -------------------------------------------------------------------------


class _Prefetched:
    """``MinuteSource`` / ``Funding`` look-alikes over data fetched for one hour."""

    def __init__(self) -> None:
        self.bars: dict[str, MinuteBars] = {}
        self.settles: dict[str, list[tuple[int, float]]] = {}
        self.mark_missing = 0

    def window(self, symbol: str, start: int, span_ms: int) -> MinuteBars:
        return self.bars.get(symbol) or _empty()

    def settlements(self, symbol: str, start: int, end: int) -> list[tuple[int, float]]:
        return [(t, r) for t, r in self.settles.get(symbol, []) if start <= t < end]


def _empty() -> MinuteBars:
    z = np.array([])
    return MinuteBars(np.array([], np.int64), z, z, z, z, z, z, z, 0)


def minute_bars(last_rows: list, mark_rows: list) -> MinuteBars:
    """Binance kline rows (lists) -> MinuteBars, mark aligned by open time."""
    t = np.array([int(r[0]) for r in last_rows], np.int64)
    o, h, lo, c = (np.array([float(r[i]) for r in last_rows]) for i in (1, 2, 3, 4))
    mark = {int(r[0]): r for r in mark_rows}
    missing = np.array([int(x) not in mark for x in t])
    mh = np.array([float(mark[x][2]) if x in mark else h[i] for i, x in enumerate(t)])
    ml = np.array([float(mark[x][3]) if x in mark else lo[i] for i, x in enumerate(t)])
    mc = np.array([float(mark[x][4]) if x in mark else c[i] for i, x in enumerate(t)])
    return MinuteBars(t, o, h, lo, c, mh, ml, mc, int(missing.sum()))


class PaperVenue:
    mode = "shadow"

    def __init__(self, trading: Any, capital: float, fee_taker: float, leverage: int) -> None:
        self.t = trading
        self.broker = Broker(capital, fee_taker, leverage)

    async def before_hour(self, hour_start: int) -> None:
        symbols = sorted(set(self.broker.pending) | set(self.broker.positions))
        data = _Prefetched()
        end = hour_start + HOUR - 1
        for sym in symbols:
            last = await self.t.klines(sym, "1m", 60, end_time=end)
            mark = await self.t.mark_klines(sym, "1m", 60, end_time=end)
            rows = [r for r in (last.body or []) if hour_start <= int(r[0]) <= end]
            mrows = [r for r in (mark.body or []) if hour_start <= int(r[0]) <= end]
            if last.ok and rows:
                data.bars[sym] = minute_bars(rows, mrows if mark.ok else [])
            fh = await self.t.funding_history(sym, hour_start, end)
            if fh.ok:
                data.settles[sym] = [
                    (int(r["fundingTime"]) // 1000 * 1000, float(r["fundingRate"]))
                    for r in fh.body or []
                ]
        self.broker.process_window(hour_start, data, data, HOUR)

    def positions(self) -> dict[str, Position]:
        return {s: sp.pos for s, sp in self.broker.positions.items()}

    def pending(self) -> dict[str, tuple[Side, float]]:
        return {s: (pe.signal.side, pe.size.notional) for s, pe in self.broker.pending.items()}

    def equity(self, marks: dict[str, float]) -> float:
        return self.broker.equity(marks)

    def margin(self) -> float:
        return self.broker.margin()

    async def open(self, sig: Signal, d: Decision, k_sym: float) -> None:
        self.broker.submit(PendingEntry(sig, d.size, d.slip, k_sym, d.size_mult, True))

    async def move_stop(self, symbol: str, price: float) -> None:
        sp = self.broker.positions.get(symbol)
        if sp is not None:
            sp.pos.stop = price

    async def exit(self, symbol: str, reason: str) -> None:
        self.broker.request_exit(symbol, reason)

    def closed_trades(self) -> list[dict]:
        return self.broker.closed


# -- live --------------------------------------------------------------------------


@dataclass
class LiveVenue:
    oms: Oms
    repo: Repo
    trading: TradingPort
    capital: float
    leverage: int
    mode: str = "live"

    async def before_hour(self, hour_start: int) -> None:
        await self.oms.drain()

    def positions(self) -> dict[str, Position]:
        out = {}
        for p in self.repo.open_positions():
            if p["status"] == "PENDING" or not p["entry_price"]:
                continue
            pos = Position(
                p["symbol"],
                Side(p["side"]),
                p["signal_id"],
                p["entry_price"],
                p["stop"],
                p["stop_initial"],
                p["tp1"],
                p["tp2"],
                p["qty"],
                p["qty_open"],
                p["opened_at"],
                tp1_done=bool(p["tp1_done"]),
                bars_open=p["bars_open"] or 0,
                best=p["best"] or p["entry_price"],
                worst=p["worst"] or p["entry_price"],
            )
            out[p["symbol"]] = pos
        return out

    def pending(self) -> dict[str, tuple[Side, float]]:
        return {
            p["symbol"]: (Side(p["side"]), p["qty"] * (p["stop_initial"] or 0))
            for p in self.repo.open_positions()
            if p["status"] == "PENDING"
        }

    def equity(self, marks: dict[str, float]) -> float:
        unreal = sum(
            p.side.sign * p.qty_open * (marks.get(s, p.entry) - p.entry)
            for s, p in self.positions().items()
        )
        return self.capital + self.repo.realized_net() + unreal

    def margin(self) -> float:
        return sum(p.qty_open * p.entry for p in self.positions().values()) / self.leverage

    async def open(self, sig: Signal, d: Decision, k_sym: float) -> None:
        await self.oms.open(sig, d.size)

    async def move_stop(self, symbol: str, price: float) -> None:
        await self.oms.move_stop(symbol, price)

    async def exit(self, symbol: str, reason: str) -> None:
        await self.oms.close(symbol, reason)

    def closed_trades(self) -> list[dict]:
        return [
            {"exit_time": p["closed_at"], "net": p["pnl"]}
            for p in self.repo.closed_positions()
            if p["entry_price"]
        ]

    def persist_excursions(self, positions: dict[str, Position]) -> None:
        for p in positions.values():
            self.repo.update_position(
                p.signal_id, best=p.best, worst=p.worst, bars_open=p.bars_open
            )
