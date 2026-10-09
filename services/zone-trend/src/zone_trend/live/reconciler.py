"""Reconciler: the database against the exchange, repaired before trading.

Runs at startup, every ``reconcile_interval_s`` and after every stream
reconnect. Only orders with the ``zt-`` prefix and symbols the bot manages
are ever touched, so a shared account's other positions are left alone
(they get a warning, once).

==========================================  ============================================
Mismatch                                    Repair
==========================================  ============================================
Fills the stream missed                     backfilled from ``userTrades`` (idempotent)
Entry outcome unknown                       queried by client id; filled -> protected,
                                            absent -> the position row is closed
DB position flat on the exchange            fills backfilled; closed in the DB
Quantity differs                            the exchange wins; alert
Exchange position without a live stop       the DB stop is placed; critical alert
Exchange position with no DB record         a stop 6% away is placed; critical alert
Orphan ``zt-`` algo order                   cancelled; alert
==========================================  ============================================
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from zone_trend.live.oms import EPS, NOT_FOUND, PREFIX, Alerts, Oms, TradingPort, _round_price
from zone_trend.persistence.repo import Repo
from zone_trend.types import Side

__all__ = ["Reconciler"]

log = logging.getLogger(__name__)

EMERGENCY_STOP_PCT = 0.06


class Reconciler:
    def __init__(
        self,
        trading: TradingPort,
        repo: Repo,
        oms: Oms,
        alerts: Alerts,
        symbols: set[str],
        clock_ms: Callable[[], int],
    ) -> None:
        self.t = trading
        self.repo = repo
        self.oms = oms
        self.alerts = alerts
        self.symbols = symbols
        self.now = clock_ms
        self._warned_foreign: set[str] = set()

    async def run_once(self) -> list[str]:
        repairs: list[str] = []
        pos_reply = await self.t.positions()
        algo_reply = await self.t.open_algo_orders()
        if not (pos_reply.ok and algo_reply.ok):
            log.warning(
                "reconcile skipped: positions %s, algo %s", pos_reply.outcome, algo_reply.outcome
            )
            return ["skipped: exchange unavailable"]
        exchange: dict[str, tuple[float, float]] = {}
        for row in pos_reply.body or []:
            amt = float(row.get("positionAmt", 0))
            if abs(amt) > EPS:
                exchange[row["symbol"]] = (amt, float(row.get("entryPrice", 0)))
        live_algos = {
            a.get("clientAlgoId"): a
            for a in (algo_reply.body or [])
            if str(a.get("clientAlgoId", "")).startswith(PREFIX)
        }

        # DB algo orders Binance no longer lists (and that did not trigger) are gone.
        for o in self.repo.open_orders():
            if o["type"] in ("STOP_MARKET", "TAKE_PROFIT_MARKET") and o["status"] == "OPEN":
                if o["client_id"] not in live_algos:
                    self.repo.update_order(o["client_id"], status="CANCELED", updated_at=self.now())

        db_open = {p["symbol"]: p for p in self.repo.open_positions()}
        for sym, pos in db_open.items():
            repairs += await self._position(pos, exchange.get(sym), live_algos)

        for sym, (amt, entry) in exchange.items():
            if sym in db_open:
                continue
            if sym not in self.symbols:
                if sym not in self._warned_foreign:
                    self._warned_foreign.add(sym)
                    await self.alerts(
                        "warning",
                        f"{sym}: position not managed by zone-trend",
                        [f"amount {amt:g}; left alone"],
                    )
                continue
            repairs.append(await self._emergency_stop(sym, amt, entry))

        db_cids = {o["client_id"] for o in self.repo.open_orders()}
        for cid in live_algos:
            if cid not in db_cids:
                await self.t.cancel_algo(cid)
                repairs.append(f"cancelled orphan {cid}")
                await self.alerts("warning", "orphan order cancelled", [cid])
        if repairs:
            self.repo.log_event(self.now(), "warning", "reconcile", repairs)
        return repairs

    async def _position(self, pos: dict, ex: tuple[float, float] | None, live: dict) -> list[str]:
        out: list[str] = []
        sym, pid = pos["symbol"], pos["id"]
        await self._backfill(pos)
        if pos["status"] == "PENDING":
            out += await self._pending(pos)
            pos = self.repo.position(pid)
            if pos is None or pos["status"] in ("CLOSED", "PENDING"):
                return out
        await self.oms.drain()
        pos = self.repo.position(pid)
        if pos["status"] == "CLOSED":
            return out
        if ex is None:
            if pos["qty_open"] > EPS:
                # Flat on the exchange, yet the fills do not add up: close it anyway.
                self.repo.update_position(pid, qty_open=0.0)
                await self.oms._finalize(pid, "reconciled_flat")
                out.append(f"{sym}: flat on the exchange, closed in the DB")
                await self.alerts(
                    "warning",
                    f"{sym} closed by reconciliation",
                    ["exchange flat; fills incomplete"],
                )
            return out
        amt = abs(ex[0])
        if abs(amt - pos["qty_open"]) > max(EPS, 1e-9 * amt):
            self.repo.update_position(pid, qty_open=amt)
            out.append(f"{sym}: qty {pos['qty_open']:g} -> {amt:g} (exchange)")
            await self.alerts(
                "warning",
                f"{sym} quantity corrected",
                [f"DB {pos['qty_open']:g}, exchange {amt:g}"],
            )
        stops = [
            o
            for o in self.repo.orders_for(pid, open_only=True)
            if o["leg"].startswith("sl") and o["client_id"] in live
        ]
        if not stops:
            async with self.oms._lock(sym):
                ok = await self.oms._protect(pid)
            out.append(f"{sym}: stop {'replaced' if ok else 'COULD NOT be placed'}")
            await self.alerts(
                "critical",
                f"{sym} was unprotected",
                ["stop placed from the DB" if ok else "stop placement failed"],
            )
        return out

    async def _backfill(self, pos: dict) -> None:
        reply = await self.t.user_trades(pos["symbol"], pos["opened_at"])
        if not reply.ok:
            return
        entry_side = "BUY" if pos["side"] == Side.LONG.value else "SELL"
        for tr in reply.body or []:
            order = self.repo.order_by_exchange_id(pos["symbol"], str(tr.get("orderId")))
            if order is not None and order["position_id"] != pos["id"]:
                continue
            if order is not None:
                leg, cid = order["leg"], order["client_id"]
            elif tr.get("side") == entry_side:
                continue  # not ours, or an entry we already know from its response
            else:
                leg, cid = "unattributed", None
            self.oms.record_fill(
                pos["id"],
                leg,
                cid,
                pos["symbol"],
                tr.get("id"),
                float(tr["price"]),
                float(tr["qty"]),
                float(tr.get("commission") or 0),
                tr.get("commissionAsset"),
                float(tr.get("realizedPnl") or 0),
                bool(tr.get("maker")),
                int(tr.get("time", 0)),
            )

    async def _pending(self, pos: dict) -> list[str]:
        cid = next(
            (o["client_id"] for o in self.repo.orders_for(pos["id"]) if o["leg"] == "entry"), None
        )
        if cid is None:
            return []
        found = await self.t.query_order(pos["symbol"], cid)
        if found.ok:
            body = found.body or {}
            self.repo.update_order(
                cid, status=body.get("status", "SENT"), exchange_id=str(body.get("orderId", ""))
            )
            if body.get("status") == "FILLED":
                async with self.oms._lock(pos["symbol"]):
                    await self.oms._entry_filled(
                        pos["id"], float(body["avgPrice"]), float(body["executedQty"])
                    )
                return [f"{pos['symbol']}: unknown entry was filled; protected"]
            return []
        if found.outcome == "rejected" and found.code == NOT_FOUND:
            self.repo.update_order(cid, status="REJECTED", last_error="absent on exchange")
            self.repo.update_position(
                pos["id"],
                status="CLOSED",
                closed_at=self.now(),
                pnl=0.0,
                exit_reason="entry_absent",
            )
            return [f"{pos['symbol']}: unknown entry never reached Binance"]
        return []

    async def _emergency_stop(self, sym: str, amt: float, entry: float) -> str:
        long = amt > 0
        price = entry * (1 - EMERGENCY_STOP_PCT if long else 1 + EMERGENCY_STOP_PCT)
        price = _round_price(price, self.oms._filters(sym).tick, up=not long)
        cid = f"{PREFIX}emerg-{sym}-{self.now() // 1000}"[:36]
        reply = await self.t.new_algo(
            sym,
            "SELL" if long else "BUY",
            "STOP_MARKET",
            trigger_price=price,
            client_id=cid,
            close_position=True,
        )
        await self.alerts(
            "critical",
            f"{sym}: position with no DB record",
            [f"amount {amt:g} @ {entry:g}", f"emergency stop {price:g}: {reply.outcome}"],
        )
        return f"{sym}: unknown position, emergency stop {reply.outcome}"
