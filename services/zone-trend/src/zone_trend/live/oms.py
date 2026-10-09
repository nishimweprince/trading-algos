"""Order manager: intent persisted before every call, never a position without
an exchange-held stop.

Lifecycle of a position (client ids ``zt-{signal hash[:12]}-{leg}``):

1. A ``PENDING`` position row and a ``NEW`` entry order row are written, then
   the MARKET entry is sent. An ambiguous reply (timeout, 5xx) is never resent
   blindly: the order is queried by client id and resent only when Binance
   confirms it does not exist.
2. On the entry fill: the stop first (STOP_MARKET, closePosition, mark price,
   price protection), then TP1 and TP2 (TAKE_PROFIT_MARKET, reduce-only,
   explicit quantities). If the stop cannot be placed twice, the position is
   closed at market and a critical alert goes out. -2021 ("would immediately
   trigger") on a stop means the stop is already through: close at market.
3. TP1 fill: a breakeven stop is placed and confirmed, then the old stop is
   cancelled; never the reverse. Trailing moves use the same place-then-cancel.
   Replacement stops alternate between closePosition and a reduce-only
   quantity stop, because Binance allows one closePosition stop per direction.
4. Exits at market (time stop, CHoCH, kill): a reduce-only MARKET close goes
   out first; the protective orders are cancelled only once the position is
   flat, so a failed close leaves the stop in place.
5. Flat: remaining algo orders are cancelled and the trade is written with
   realized PnL, commissions and funding from Binance's own ledger.

Fills come from the user stream (``ORDER_TRADE_UPDATE``); an algo order that
triggers is linked to the order it spawned through ``ALGO_UPDATE``. Anything
the stream misses is repaired by the reconciler.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from zone_trend.persistence.repo import Repo
from zone_trend.risk.liquidation import liquidation_price
from zone_trend.risk.sizing import Filters, Size
from zone_trend.types import Side, Signal

__all__ = ["Alerts", "Oms", "TradingPort", "client_id"]

log = logging.getLogger(__name__)

PREFIX = "zt-"
EPS = 1e-12
NOT_FOUND = -2013
WOULD_TRIGGER = -2021
STOP_ATTEMPTS = 2


def client_id(signal_id: str, leg: str) -> str:
    return f"{PREFIX}{signal_id[:12]}-{leg}"


class TradingPort(Protocol):
    """The ``FuturesTrading`` calls the OMS and reconciler use (faked in tests)."""

    async def new_order(self, symbol: str, side: str, order_type: str, **kw: Any) -> Any: ...
    async def query_order(self, symbol: str, client_id: str) -> Any: ...
    async def new_algo(self, symbol: str, side: str, order_type: str, **kw: Any) -> Any: ...
    async def query_algo(self, client_id: str) -> Any: ...
    async def cancel_algo(self, client_id: str) -> Any: ...
    async def open_algo_orders(self, symbol: str | None = None) -> Any: ...
    async def positions(self, symbol: str | None = None) -> Any: ...
    async def user_trades(self, symbol: str, start_time: int | None = None) -> Any: ...
    async def income(
        self, symbol: str, income_type: str = ..., start_time: int | None = ...
    ) -> Any: ...


class Alerts(Protocol):
    async def __call__(self, level: str, subject: str, lines: list[str]) -> None: ...


def _round_step(qty: float, step: float) -> float:
    return math.floor(qty / step + 1e-9) * step if step > 0 else qty


def _round_price(price: float, tick: float, up: bool) -> float:
    if tick <= 0:
        return price
    n = price / tick
    return (math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)) * tick


def _close_side(side: str) -> str:
    return "SELL" if side == Side.LONG.value else "BUY"


class Oms:
    def __init__(
        self,
        trading: TradingPort,
        repo: Repo,
        alerts: Alerts,
        *,
        leverage: int,
        fee_taker: float,
        clock_ms: Callable[[], int],
    ) -> None:
        self.t = trading
        self.repo = repo
        self.alerts = alerts
        self.leverage = leverage
        self.fee = fee_taker
        self.now = clock_ms
        self.filters: dict[str, Filters] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()

    def _lock(self, symbol: str) -> asyncio.Lock:
        return self._locks.setdefault(symbol, asyncio.Lock())

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for follow-ups scheduled by stream events (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks))

    # -- entry --------------------------------------------------------------------
    async def open(self, sig: Signal, size: Size) -> bool:
        """Send the entry; True once it is filled and protected (or being protected)."""
        sym = sig.symbol
        async with self._lock(sym):
            if self.repo.open_position_for(sym):
                return False
            now = self.now()
            self.repo.insert_position(
                id=sig.id,
                symbol=sym,
                side=sig.side.value,
                signal_id=sig.id,
                entry_type=sig.entry_type,
                qty=size.qty,
                qty_open=0.0,
                tp1_qty=size.tp1_qty,
                single_tp=int(size.single_tp),
                leverage=self.leverage,
                stop=sig.stop,
                stop_initial=sig.stop,
                tp1=sig.tp1,
                tp2=sig.tp2,
                tp1_done=0,
                bars_open=0,
                status="PENDING",
                opened_at=now,
                fees=0.0,
                funding=0.0,
                mode="live",
            )
            cid = client_id(sig.id, "entry")
            side = "BUY" if sig.side is Side.LONG else "SELL"
            self.repo.insert_order(
                client_id=cid,
                signal_id=sig.id,
                position_id=sig.id,
                symbol=sym,
                leg="entry",
                type="MARKET",
                side=side,
                qty=size.qty,
                reduce_only=0,
                close_position=0,
                status="NEW",
                created_at=now,
                updated_at=now,
            )
            reply = await self._send_entry(sym, side, size.qty, cid)
            if reply is None:
                return False
            body = reply.body or {}
            self.repo.update_order(
                cid, status="SENT", exchange_id=str(body.get("orderId", "")), updated_at=self.now()
            )
            if body.get("status") == "FILLED":
                await self._entry_filled(
                    sig.id, float(body["avgPrice"]), float(body["executedQty"])
                )
            return True

    async def _send_entry(self, sym: str, side: str, qty: float, cid: str):
        reply = await self.t.new_order(sym, side, "MARKET", quantity=qty, client_id=cid)
        for _ in range(2):
            if reply.outcome != "unknown":
                break
            found = await self.t.query_order(sym, cid)
            if found.ok:
                reply = found
                break
            if found.outcome == "rejected" and found.code == NOT_FOUND:
                reply = await self.t.new_order(sym, side, "MARKET", quantity=qty, client_id=cid)
                continue
        if reply.outcome == "ok":
            return reply
        pid = self.repo.order(cid)["position_id"]
        if reply.outcome == "unknown":
            self.repo.update_order(
                cid, status="UNKNOWN", last_error=str(reply.msg), updated_at=self.now()
            )
            await self.alerts(
                "critical",
                f"entry {cid} outcome unknown",
                ["the reconciler will resolve it; check the account"],
            )
            return None
        self.repo.update_order(
            cid, status="REJECTED", last_error=f"{reply.code} {reply.msg}", updated_at=self.now()
        )
        self.repo.update_position(
            pid,
            status="CLOSED",
            closed_at=self.now(),
            pnl=0.0,
            exit_reason=f"entry_{reply.outcome}:{reply.code}",
        )
        return None

    async def _entry_filled(self, pid: str, price: float, qty: float) -> None:
        pos = self.repo.position(pid)
        if pos is None or pos["status"] != "PENDING":
            return  # already handled (response and stream both report the fill)
        sign = 1 if pos["side"] == Side.LONG.value else -1
        self.repo.update_position(
            pid,
            status="OPEN",
            entry_price=price,
            qty=qty,
            qty_open=qty,
            best=price,
            worst=price,
            opened_at=self.now(),
            liq_price=liquidation_price(price, qty, sign, self.leverage),
        )
        if not await self._protect(pid):
            await self.alerts(
                "critical", f"{pos['symbol']} stop failed twice: closing", [f"position {pid}"]
            )
            await self._close_locked(pid, "stop_failed")
            return
        await self._place_targets(pid)
        pos = self.repo.position(pid)
        await self.alerts(
            "info",
            f"{pos['symbol']} {pos['side']} filled",
            [
                f"entry {price:g} qty {qty:g}",
                f"stop {pos['stop']:g} ({abs(price - pos['stop']) / price:.2%})",
                f"risk ${qty * abs(price - pos['stop']):.2f}",
            ],
        )

    # -- protective orders ---------------------------------------------------------
    def _filters(self, sym: str) -> Filters:
        return self.filters.get(sym, Filters(0.0, 0.0, 0.0, 0.0))

    def _next_stop_leg(self, pid: str) -> str:
        n = sum(1 for o in self.repo.orders_for(pid) if o["leg"].startswith("sl"))
        return f"sl{n + 1}"

    async def _protect(self, pid: str, price: float | None = None) -> bool:
        """Place a stop (at ``price`` or the position's stop); True when it is live."""
        pos = self.repo.position(pid)
        sym, long = pos["symbol"], pos["side"] == Side.LONG.value
        trigger = _round_price(price or pos["stop"], self._filters(sym).tick, up=not long)
        # Binance allows one closePosition stop per direction (-4130), so a
        # replacement alternates with a reduce-only stop for the open quantity;
        # both can be live during the place-then-cancel overlap.
        old_row = self.repo.order(pos["stop_leg"]) if pos["stop_leg"] else None
        close_pos = not (old_row and old_row["close_position"])
        qty = None if close_pos else _round_step(pos["qty_open"], self._filters(sym).step)
        for _ in range(STOP_ATTEMPTS):
            leg = self._next_stop_leg(pid)
            reply = await self._algo(
                pid, leg, "STOP_MARKET", trigger, close_position=close_pos, quantity=qty
            )
            if reply.ok:
                old = pos["stop_leg"]
                self.repo.update_position(pid, stop=trigger, stop_leg=client_id(pid, leg))
                if old:
                    await self._cancel_algo(old)
                return True
            if reply.outcome == "rejected" and reply.code == WOULD_TRIGGER:
                await self._close_locked(pid, "stop")
                return True
        return False

    async def _place_targets(self, pid: str) -> None:
        pos = self.repo.position(pid)
        f = self._filters(pos["symbol"])
        long = pos["side"] == Side.LONG.value
        qty = pos["qty_open"]
        if pos["tp1"] is not None and not math.isnan(pos["tp1"]):
            q1 = qty if pos["single_tp"] else min(_round_step(pos["tp1_qty"], f.step), qty)
            tp1 = _round_price(pos["tp1"], f.tick, up=not long)
            await self._algo(pid, "tp1", "TAKE_PROFIT_MARKET", tp1, quantity=q1)
            rest = _round_step(qty - q1, f.step)
            if not pos["single_tp"] and pos["tp2"] is not None and rest > EPS:
                tp2 = _round_price(pos["tp2"], f.tick, up=not long)
                await self._algo(pid, "tp2", "TAKE_PROFIT_MARKET", tp2, quantity=rest)

    async def _algo(
        self,
        pid: str,
        leg: str,
        kind: str,
        trigger: float,
        *,
        close_position: bool = False,
        quantity: float | None = None,
    ):
        pos = self.repo.position(pid)
        cid = client_id(pid, leg)
        side = _close_side(pos["side"])
        now = self.now()
        self.repo.insert_order(
            client_id=cid,
            signal_id=pid,
            position_id=pid,
            symbol=pos["symbol"],
            leg=leg,
            type=kind,
            side=side,
            qty=quantity,
            trigger_price=trigger,
            reduce_only=int(not close_position),
            close_position=int(close_position),
            status="NEW",
            created_at=now,
            updated_at=now,
        )
        kw: dict[str, Any] = {"trigger_price": trigger, "client_id": cid}
        if close_position:
            kw["close_position"] = True
        else:
            kw.update(quantity=quantity, reduce_only=True)
        reply = await self.t.new_algo(pos["symbol"], side, kind, **kw)
        if reply.outcome == "unknown":
            found = await self.t.query_algo(cid)
            reply = found if found.ok else reply
        if reply.ok:
            algo_id = str((reply.body or {}).get("algoId", ""))
            self.repo.update_order(cid, status="OPEN", algo_id=algo_id, updated_at=self.now())
        else:
            status = "UNKNOWN" if reply.outcome == "unknown" else "REJECTED"
            self.repo.update_order(
                cid, status=status, last_error=f"{reply.code} {reply.msg}", updated_at=self.now()
            )
            log.warning("algo %s %s: %s %s", cid, reply.outcome, reply.code, reply.msg)
        return reply

    async def _cancel_algo(self, cid: str) -> None:
        reply = await self.t.cancel_algo(cid)
        if reply.ok or (reply.outcome == "rejected"):
            # rejected: already gone (triggered or cancelled); either way not open
            self.repo.update_order(cid, status="CANCELED", updated_at=self.now())

    # -- management -------------------------------------------------------------------
    async def move_stop(self, symbol: str, price: float) -> bool:
        async with self._lock(symbol):
            pos = self.repo.open_position_for(symbol)
            if pos is None or pos["status"] != "OPEN":
                return False
            ok = await self._protect(pos["id"], price)
            if not ok:
                await self.alerts(
                    "warning", f"{symbol} stop move failed", [f"kept the old stop {pos['stop']:g}"]
                )
            return ok

    async def close(self, symbol: str, reason: str) -> None:
        async with self._lock(symbol):
            pos = self.repo.open_position_for(symbol)
            if pos is not None:
                await self._close_locked(pos["id"], reason)

    async def _close_locked(self, pid: str, reason: str) -> None:
        pos = self.repo.position(pid)
        if pos is None or pos["qty_open"] <= EPS:
            return
        n = sum(1 for o in self.repo.orders_for(pid) if o["leg"].startswith("close"))
        cid = client_id(pid, f"close{n + 1}")
        now = self.now()
        self.repo.update_position(pid, status="CLOSING", exit_reason=reason)
        self.repo.insert_order(
            client_id=cid,
            signal_id=pid,
            position_id=pid,
            symbol=pos["symbol"],
            leg=f"close{n + 1}",
            type="MARKET",
            side=_close_side(pos["side"]),
            qty=pos["qty_open"],
            reduce_only=1,
            close_position=0,
            status="NEW",
            created_at=now,
            updated_at=now,
        )
        reply = await self.t.new_order(
            pos["symbol"],
            _close_side(pos["side"]),
            "MARKET",
            quantity=pos["qty_open"],
            client_id=cid,
            reduce_only=True,
        )
        if reply.outcome == "unknown":
            found = await self.t.query_order(pos["symbol"], cid)
            reply = found if found.ok else reply
        if reply.ok:
            # Quantity is counted from fills (stream or reconciler), never from here.
            self.repo.update_order(cid, status="SENT", updated_at=self.now())
        else:
            self.repo.update_order(
                cid,
                status="UNKNOWN" if reply.outcome == "unknown" else "REJECTED",
                last_error=f"{reply.code} {reply.msg}",
            )
            self.repo.update_position(pid, status="OPEN")  # the stop is still in place
            await self.alerts(
                "critical",
                f"{pos['symbol']} close failed ({reason})",
                [
                    f"{reply.outcome} {reply.code} {reply.msg}",
                    "the exchange stop is still in place",
                ],
            )

    def record_fill(
        self,
        pid: str,
        leg: str,
        cid: str | None,
        sym: str,
        trade_id: Any,
        price: float,
        qty: float,
        fee: float,
        fee_asset: str | None,
        realized: float,
        maker: bool,
        ts: int,
    ) -> None:
        """Record one fill (idempotent by trade id) and act on the new open quantity.

        Used by the stream handler and by the reconciler's REST backfill, so a
        fill seen twice counts once.
        """
        new = self.repo.insert_fill(
            id=f"{sym}:{trade_id}",
            client_id=cid,
            position_id=pid,
            leg=leg,
            price=price,
            qty=qty,
            fee=fee,
            fee_asset=fee_asset,
            realized_pnl=realized,
            maker=int(maker),
            ts=ts,
        )
        if not new or leg == "entry":
            return
        pos = self.repo.position(pid)
        if pos is None or pos["status"] in ("CLOSED", "PENDING"):
            return
        exited = sum(f["qty"] for f in self.repo.fills_for(pid) if f["leg"] != "entry")
        left = max(pos["qty"] - exited, 0.0)
        self.repo.update_position(pid, qty_open=left)
        if leg == "tp1" and left > EPS:
            self.repo.update_position(pid, tp1_done=1)
            self._spawn(self._breakeven(pos["symbol"], pid))
        if left <= EPS:
            self._spawn(self._finalize(pid, leg))

    # -- stream ------------------------------------------------------------------------
    def on_event(self, ev: dict[str, Any]) -> None:
        kind = ev.get("e")
        if kind == "ORDER_TRADE_UPDATE":
            self._on_order(ev.get("o", {}), int(ev.get("T") or ev.get("E") or self.now()))
        elif kind == "ALGO_UPDATE":
            self._on_algo(ev.get("o") or ev.get("ao") or {})

    def _on_algo(self, a: dict[str, Any]) -> None:
        cid = a.get("caid") or ""
        if not cid.startswith(PREFIX) or self.repo.order(cid) is None:
            return
        status = a.get("X", "")
        fields: dict[str, Any] = {"updated_at": self.now()}
        if a.get("ai"):
            fields["exchange_id"] = str(a["ai"])
        if status in ("TRIGGERED", "FINISHED"):
            fields["status"] = "TRIGGERED"
        elif status in ("CANCELED", "EXPIRED", "REJECTED"):
            fields["status"] = status if status != "EXPIRED" else "CANCELED"
        self.repo.update_order(cid, **fields)

    def _order_for(self, o: dict[str, Any]) -> dict | None:
        cid = o.get("c") or ""
        if cid.startswith(PREFIX):
            return self.repo.order(cid)
        return self.repo.order_by_exchange_id(o.get("s", ""), str(o.get("i", "")))

    def _on_order(self, o: dict[str, Any], ts: int) -> None:
        sym = o.get("s", "")
        order = self._order_for(o)
        if order is None:
            pos = self.repo.open_position_for(sym)
            if pos is None or not o.get("R"):
                return  # not ours
            order = {"client_id": None, "position_id": pos["id"], "leg": "unattributed"}
        pid, leg = order["position_id"], order["leg"]
        if order["client_id"] and o.get("X") in ("FILLED", "CANCELED", "EXPIRED"):
            self.repo.update_order(order["client_id"], status=o["X"], updated_at=self.now())
        if o.get("x") != "TRADE":
            return
        qty, price = float(o.get("l", 0)), float(o.get("L", 0))
        self.record_fill(
            pid,
            leg,
            order["client_id"],
            sym,
            o.get("t"),
            price,
            qty,
            float(o.get("n") or 0),
            o.get("N"),
            float(o.get("rp") or 0),
            bool(o.get("m")),
            ts,
        )
        if leg == "entry" and o.get("X") == "FILLED":
            self._spawn(
                self._entry_from_stream(pid, float(o.get("ap") or price), float(o.get("z") or qty))
            )

    async def _entry_from_stream(self, pid: str, price: float, qty: float) -> None:
        pos = self.repo.position(pid)
        if pos is None:
            return
        async with self._lock(pos["symbol"]):
            await self._entry_filled(pid, price, qty)

    async def _breakeven(self, symbol: str, pid: str) -> None:
        async with self._lock(symbol):
            pos = self.repo.position(pid)
            if pos is None or pos["status"] != "OPEN":
                return
            sign = 1 if pos["side"] == Side.LONG.value else -1
            be = pos["entry_price"] * (1 + sign * 2 * self.fee)
            if sign * (pos["stop"] - be) < 0:
                await self._protect(pid, be)

    async def _finalize(self, pid: str, last_leg: str | None = None) -> None:
        pos = self.repo.position(pid)
        if pos is None or pos["status"] == "CLOSED":
            return
        async with self._lock(pos["symbol"]):
            pos = self.repo.position(pid)
            if pos["status"] == "CLOSED":
                return
            for o in self.repo.orders_for(pid, open_only=True):
                if o["type"] in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
                    await self._cancel_algo(o["client_id"])
            fills = self.repo.fills_for(pid)
            fees = sum(f["fee"] for f in fills if (f["fee_asset"] or "USDT") == "USDT")
            realized = sum(f["realized_pnl"] for f in fills)
            funding = 0.0
            inc = await self.t.income(pos["symbol"], "FUNDING_FEE", pos["opened_at"])
            if inc.ok and isinstance(inc.body, list):
                funding = sum(float(r.get("income", 0)) for r in inc.body)
            net = realized - fees + funding
            reason = pos["exit_reason"] or _reason_from_leg(last_leg)
            self.repo.update_position(
                pid,
                status="CLOSED",
                qty_open=0.0,
                closed_at=self.now(),
                pnl=net,
                fees=fees,
                funding=-funding,
                exit_reason=reason,
            )
            risk = pos["qty"] * abs((pos["entry_price"] or 0) - pos["stop_initial"])
            r = net / risk if risk else float("nan")
            await self.alerts(
                "info",
                f"{pos['symbol']} closed: {reason}",
                [f"net {net:+.2f} USDT ({r:+.2f}R)", f"fees {fees:.2f}, funding {funding:+.2f}"],
            )


def _reason_from_leg(leg: str | None) -> str:
    if leg is None:
        return "closed"
    if leg.startswith("sl"):
        return "stop"
    if leg.startswith("close"):
        return "market_close"
    return leg
