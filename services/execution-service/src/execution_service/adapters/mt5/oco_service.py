"""Gateway-owned OCO coordination; no candle loop participates in cancellation.

Entries are never resent after dispatch begins. Cancel and protection operations
are reconciled against live state. Netting is deliberately unsupported: recovery
must be able to close an owned losing position without reversing shared exposure.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

from ta_contracts import SignalRequest
from ta_core import ServiceError

from .oco_models import OcoGroupRequest
from .oco_repository import OcoRepository
from .service import SignalExecutionService

TERMINAL_LEGS = {"not_submitted", "cancelled", "expired", "rejected", "closed"}
UNCERTAIN_LEGS = {"dispatching", "unknown"}


class Mt5OcoService:
    def __init__(self, signals: SignalExecutionService, repository: OcoRepository) -> None:
        self.signals = signals
        self.adapter = signals.adapter
        self.settings = signals.settings
        self.repository = repository
        self.recovered = False
        self.monitor_error: str | None = "startup reconciliation pending"
        self.last_scan: float | None = None

    def _guard(self, symbol: str | None = None) -> None:
        if not self.settings.mt5_oco_enabled:
            raise ServiceError(503, "oco_disabled", "MT5_OCO_ENABLED is false")
        self.signals._ensure_ready()
        account = self.adapter.account_metadata()
        if (
            account.get("login") != self.settings.login
            or account.get("margin_mode") != 2
            or account.get("server") != self.settings.server
        ):
            raise ServiceError(
                422, "oco_account_unsupported", "OCO requires the configured hedge account"
            )
        if symbol is not None:
            info = self.adapter.symbol_info(symbol)
            if info is None or not info.expiration_mode & 4:
                raise ServiceError(
                    422, "oco_expiry_unsupported", "Symbol must support specified expiry"
                )
            tick = self.adapter.symbol_tick(symbol)
            offset = self.settings.mt5_oco_server_utc_offset_seconds
            age = (
                datetime.now(UTC).timestamp() - (tick.time - offset)
                if tick is not None and tick.time is not None
                else float("inf")
            )
            if not -5 <= age <= 60:
                raise ServiceError(
                    503,
                    "oco_server_clock_unverified",
                    "Fresh server quote must match MT5_OCO_SERVER_UTC_OFFSET_SECONDS",
                )

    async def capabilities(self, symbol: str | None = None) -> dict[str, Any]:
        reason = "ready"
        ready = False
        try:
            async with self.signals._terminal_lock:
                await asyncio.to_thread(self._guard, symbol)
                groups = await asyncio.to_thread(self.repository.all)
            fresh = self.last_scan is not None and time.monotonic() - self.last_scan < 10
            ready = self.recovered and fresh and self.monitor_error is None
            if any(group.get("fault") or self._uncertain(group) for group in groups):
                ready = False
                reason = "owned OCO exposure needs reconciliation"
            elif not ready:
                reason = self.monitor_error or "OCO monitor is not current"
        except Exception as exc:
            reason = exc.code if isinstance(exc, ServiceError) else type(exc).__name__
        return {
            "version": 1,
            "profile": self.settings.profile,
            "ready": ready,
            "reason": reason,
            "capabilities": {
                "market_entry": True,
                "pending_entry": True,
                "cancellation": True,
                "inventory": True,
                "protection_amendment": True,
                "oco_coordination": True,
            },
        }

    @staticmethod
    def _uncertain(group: dict[str, Any]) -> bool:
        return any(leg["state"] in UNCERTAIN_LEGS for leg in group["legs"].values())

    def _verify_account(self, document: dict[str, Any]) -> None:
        account = self.adapter.account_metadata()
        if (
            account.get("login") != self.settings.login
            or account.get("margin_mode") != 2
            or document.get("account_login") != account.get("login")
            or document.get("account_server") != account.get("server")
            or account.get("server") != self.settings.server
        ):
            raise ServiceError(
                409, "oco_account_changed", "Owned group belongs to a different account"
            )

    async def submit(self, request: OcoGroupRequest) -> dict[str, Any]:
        async with self.signals._terminal_lock:
            return await asyncio.to_thread(self._submit, request)

    def _submit(self, request: OcoGroupRequest) -> dict[str, Any]:
        document = request.model_dump(mode="json")
        payload_hash = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        group_id = str(request.group_id)
        existing = self.repository.get(group_id)
        if existing is not None:
            if existing[0] != payload_hash:
                raise ServiceError(409, "idempotency_conflict", "group_id has a different payload")
            return existing[1]
        self._guard(request.symbol)
        if request.profile != self.settings.profile:
            raise ServiceError(
                422, "oco_profile_mismatch", "Request targets a different MT5 profile"
            )
        if not self.recovered or self.monitor_error is not None:
            raise ServiceError(
                503, "oco_not_reconciled", "OCO monitor has not reconciled broker state"
            )
        if self.last_scan is None or time.monotonic() - self.last_scan >= 10:
            raise ServiceError(503, "oco_monitor_stale", "OCO monitor has not scanned recently")
        if any(group.get("fault") or self._uncertain(group) for group in self.repository.all()):
            raise ServiceError(
                409, "oco_unresolved_exposure", "Resolve existing OCO exposure first"
            )
        document.update(
            state="staging",
            winner=None,
            cancel_requested=False,
            cancel_reason=None,
            fault=None,
            placement_complete=False,
            engine_prediction=None,
            created_at=datetime.now(UTC).isoformat(),
        )
        document["account_login"] = self.settings.login
        document["account_server"] = self.settings.server
        document["account_currency"] = self.adapter.account_metadata().get("currency")
        document["server_utc_offset_seconds"] = self.settings.mt5_oco_server_utc_offset_seconds
        document["legs"] = {
            side: {
                "state": "not_submitted",
                "signal_id": str(uuid5(request.group_id, side)),
                "broker_tag": f"oco-{request.group_id.hex[:20]}-{'b' if side == 'long' else 's'}",
                "order_id": None,
                "fill_price": None,
                "executed_volume": "0",
                "position_ids": [],
                "applied_protection": {},
            }
            for side in ("long", "short")
        }
        if not self.repository.reserve(group_id, payload_hash, document):
            raise ServiceError(409, "oco_in_progress", "OCO group is being processed")
        prepared: list[tuple[str, dict[str, Any]]] = []
        try:
            for side in ("long", "short"):
                leg = document["legs"][side]
                signal = self._signal(request, side)
                self.signals._validate_source(signal)
                self.signals._validate_freshness(signal)
                self.signals._validate_freshness(
                    signal.model_copy(update={"occurred_at": request.decision_at})
                )
                info, _, entry, sl, tp = self.signals._symbol_context(signal)
                if self.signals._stop_adjustments.pop(str(signal.signal_id), None):
                    raise ServiceError(
                        422, "oco_protection_widened", "Requested protection requires widening"
                    )
                order = self.signals._build_request(signal, leg["broker_tag"], info, entry, sl, tp)
                # MT5 pending orders use RETURN; filling flags govern later market execution.
                order["type_filling"] = self.adapter.constants.order_filling_return
                # The group/watchdog stays in UTC; MT5 pending expiry uses server time.
                order["expiration"] += document["server_utc_offset_seconds"]
                check = self.adapter.order_check(order)
                if check is None or int(check.get("retcode", -1)) != 0:
                    raise ServiceError(
                        422, "oco_preflight_rejected", "Both OCO legs must pass preflight"
                    )
                leg["request"] = order
                leg["applied_protection"] = {
                    "stop_loss": str(sl),
                    "take_profit": str(tp),
                    "anchor": "pending_entry",
                }
                prepared.append((side, order))
        except Exception as exc:
            document["state"] = "rejected"
            document["reason"] = exc.code if isinstance(exc, ServiceError) else type(exc).__name__
            for leg in document["legs"].values():
                leg["state"] = "rejected"
            self.repository.save(document)
            return document
        self.repository.save(document)
        for side, order in prepared:
            leg = document["legs"][side]
            if document["winner"] is not None or document["cancel_requested"]:
                break
            leg["state"] = "dispatching"
            self.repository.save(document)
            try:
                result = self.adapter.order_send(order)
            except Exception as exc:
                result = None
                leg["reason"] = type(exc).__name__
            leg["result"] = result
            if result is None:
                leg["state"] = "unknown"
                document["cancel_requested"] = True
                document["cancel_reason"] = "incomplete_placement"
            elif (
                int(result.get("retcode", -1))
                in {
                    self.adapter.constants.retcode_placed,
                    self.adapter.constants.retcode_done,
                    self.adapter.constants.retcode_done_partial,
                }
                and int(result.get("order", 0)) > 0
            ):
                leg["order_id"] = int(result["order"])
                leg["state"] = "placed"
            else:
                leg["state"] = "rejected"
                document["cancel_requested"] = True
                document["cancel_reason"] = "incomplete_placement"
            self.repository.save(document)
            try:
                self._poll_group(document, *self._inventory(document))
            except Exception as exc:
                document["cancel_requested"] = True
                document["cancel_reason"] = "inventory_unavailable"
                self.monitor_error = type(exc).__name__
                self.repository.save(document)
                break
        document["placement_complete"] = True
        self.repository.save(document)
        return document

    @staticmethod
    def _signal(request: OcoGroupRequest, side: str) -> SignalRequest:
        return SignalRequest(
            signal_id=uuid5(request.group_id, side),
            occurred_at=request.occurred_at,
            execution_type="stop",
            symbol=request.symbol,
            direction="buy" if side == "long" else "sell",
            volume=request.volume,
            entry_price=request.upper_trigger if side == "long" else request.lower_trigger,
            stop_loss_distance=request.stop_distance,
            take_profit_distance=request.target_distance,
            expires_at=request.expires_at,
            source=request.source,
            ignore_signal_age=False,
        )

    async def get(self, group_id: UUID) -> dict[str, Any]:
        record = await asyncio.to_thread(self.repository.get, str(group_id))
        if record is None:
            raise ServiceError(404, "oco_not_found", "No OCO group has this ID")
        return record[1]

    async def cancel(self, group_id: UUID, reason: str) -> dict[str, Any]:
        async with self.signals._terminal_lock:
            document = await self.get(group_id)
            await asyncio.to_thread(self._verify_account, document)
            document["cancel_requested"] = True
            document["cancel_reason"] = reason
            await asyncio.to_thread(self.repository.save, document)
            await asyncio.to_thread(
                self._poll_group, document, *await asyncio.to_thread(self._inventory, document)
            )
            return document

    def _inventory(self, document: dict[str, Any]) -> tuple[list[dict[str, Any]], ...]:
        start = datetime.fromisoformat(document["created_at"]) - timedelta(minutes=5)
        end = datetime.now(UTC) + timedelta(minutes=1)
        # Include UTC and server representations: terminal history differs by broker.
        # Ownership matching prevents the wider query from claiming other trades.
        offsets = (
            0,
            document.get("server_utc_offset_seconds", 0),
            self.settings.mt5_oco_server_utc_offset_seconds,
        )
        start += timedelta(seconds=min(offsets))
        end += timedelta(seconds=max(offsets))
        return (
            self.adapter.active_orders(),
            self.adapter.active_positions(),
            self.adapter.history_orders(start, end),
            self.adapter.history_deals(start, end),
        )

    def _owned(self, record: dict[str, Any], document: dict[str, Any]) -> bool:
        return (
            record.get("magic") == self.settings.magic_number
            and record.get("symbol") == document["symbol"]
        )

    def _poll_group(
        self,
        document: dict[str, Any],
        orders: list[dict[str, Any]],
        positions: list[dict[str, Any]],
        history: list[dict[str, Any]],
        deals: list[dict[str, Any]],
    ) -> None:
        request = OcoGroupRequest.model_validate(
            {key: document[key] for key in OcoGroupRequest.model_fields}
        )
        for _side, leg in document["legs"].items():
            matching = [
                row
                for row in orders + history
                if self._owned(row, document) and row.get("comment") == leg["broker_tag"]
            ]
            if leg["order_id"] is None and matching:
                tickets = {int(row["ticket"]) for row in matching}
                if len(tickets) != 1:
                    document["fault"] = "duplicate_owned_leg"
                    continue
                leg["order_id"] = tickets.pop()
            ticket = leg["order_id"]
            if ticket is None:
                leg["resting"] = None if leg["state"] in UNCERTAIN_LEGS else False
                continue
            entry_deals = [
                row
                for row in deals
                if self._owned(row, document)
                and row.get("order") == ticket
                and row.get("entry") == 0
            ]
            live = next(
                (
                    row
                    for row in orders
                    if row.get("ticket") == ticket and self._owned(row, document)
                ),
                None,
            )
            terminal = next(
                (
                    row
                    for row in history
                    if row.get("ticket") == ticket and self._owned(row, document)
                ),
                None,
            )
            if entry_deals:
                volume = sum(Decimal(str(row["volume"])) for row in entry_deals)
                price = (
                    sum(
                        Decimal(str(row["price"])) * Decimal(str(row["volume"]))
                        for row in entry_deals
                    )
                    / volume
                )
                leg.update(
                    executed_volume=str(volume),
                    fill_price=str(price),
                    position_ids=sorted({int(row["position_id"]) for row in entry_deals}),
                    filled_at_msc=min(
                        int(row.get("time_msc", row.get("time", 0) * 1000)) for row in entry_deals
                    )
                    - document.get("server_utc_offset_seconds", 0) * 1000,
                    state="partially_filled" if volume < request.volume else "filled",
                )
                owned_positions = [
                    row
                    for row in positions
                    if self._owned(row, document)
                    and row.get("identifier", row.get("ticket")) in leg["position_ids"]
                ]
                exits = [
                    row
                    for row in deals
                    if row.get("symbol") == document["symbol"]
                    and row.get("position_id") in leg["position_ids"]
                    and row.get("entry") in {1, 3}
                ]
                accounting_deals = entry_deals + exits
                leg["accounting"] = {
                    key: str(sum(Decimal(str(row.get(key, 0))) for row in accounting_deals))
                    for key in ("profit", "commission", "swap", "fee")
                }
                leg["accounting"]["realized_net_pnl"] = str(
                    sum(
                        Decimal(leg["accounting"][key])
                        for key in ("profit", "commission", "swap", "fee")
                    )
                )
                if (
                    not owned_positions
                    and live is None
                    and sum(Decimal(str(row["volume"])) for row in exits) >= volume
                ):
                    leg["state"] = "closed"
            elif live is not None:
                leg["state"] = "placed"
            elif terminal is not None and terminal.get("state") in {2, 5, 6}:
                leg["state"] = {2: "cancelled", 5: "rejected", 6: "expired"}[terminal["state"]]
            elif leg["state"] not in TERMINAL_LEGS:
                leg["state"] = "unknown"
            leg["resting"] = None if leg["state"] in UNCERTAIN_LEGS else live is not None
        filled = [
            (side, leg)
            for side, leg in document["legs"].items()
            if Decimal(leg["executed_volume"]) > 0
        ]
        if filled:
            document["winner"] = min(
                filled, key=lambda item: (item[1]["filled_at_msc"], 0 if item[0] == "long" else 1)
            )[0]
            sibling = document["legs"]["short" if document["winner"] == "long" else "long"]
            if sibling["state"] in {"cancelled", "expired", "rejected", "not_submitted"}:
                if document.get("sibling_cancelled_at") is None:
                    document["sibling_cancelled_at"] = datetime.now(UTC).isoformat()
                    winner_leg = document["legs"][document["winner"]]
                    document["sibling_cancel_latency_seconds"] = max(
                        0, datetime.now(UTC).timestamp() - winner_leg["filled_at_msc"] / 1000
                    )
        if len(filled) > 1 and document.get("acknowledged_fault") != "both_legs_filled":
            document["fault"] = "both_legs_filled"
            document["cancel_requested"] = True
        if datetime.now(UTC) >= request.expires_at:
            document["cancel_requested"] = True
            document["cancel_reason"] = document.get("cancel_reason") or "expired"
        self.repository.save(document)
        for side, leg in document["legs"].items():
            live = next(
                (
                    row
                    for row in orders
                    if row.get("ticket") == leg["order_id"] and self._owned(row, document)
                ),
                None,
            )
            should_cancel = document["cancel_requested"] or document["winner"] is not None
            if live is not None and should_cancel:
                self._cancel_leg(document, leg, live)
            if Decimal(leg["executed_volume"]) > 0 and leg["state"] != "closed":
                if document.get("fault") == "both_legs_filled" and side != document["winner"]:
                    self._close_loser(document, leg, positions)
                else:
                    self._protect_fill(document, side, leg, positions, request)
        states = {leg["state"] for leg in document["legs"].values()}
        if document.get("fault"):
            document["state"] = "halted"
        elif states <= TERMINAL_LEGS:
            document["state"] = (
                "closed"
                if filled
                else ("expired" if document.get("cancel_reason") == "expired" else "cancelled")
            )
        elif self._uncertain(document):
            document["state"] = "unknown"
        elif document["winner"] is not None:
            document["state"] = "filled"
        elif document["cancel_requested"]:
            document["state"] = "cancelling"
        else:
            document["state"] = "placed"
        document["updated_at"] = datetime.now(UTC).isoformat()
        self.repository.save(document)

    def _cancel_leg(
        self, document: dict[str, Any], leg: dict[str, Any], live: dict[str, Any]
    ) -> None:
        leg["cancel_intent"] = {"order_id": live["ticket"], "at": datetime.now(UTC).isoformat()}
        self.repository.save(document)
        try:
            result = self.adapter.order_send(
                {
                    "action": self.adapter.constants.trade_action_remove,
                    "order": live["ticket"],
                    "symbol": document["symbol"],
                }
            )
            leg["cancel_result"] = result
        except Exception as exc:
            leg["cancel_result"] = {"unknown": type(exc).__name__}
        # Even DONE is an operation result; live inventory/history confirms cancellation.
        self.repository.save(document)

    def _protect_fill(
        self,
        document: dict[str, Any],
        side: str,
        leg: dict[str, Any],
        positions: list[dict[str, Any]],
        request: OcoGroupRequest,
    ) -> None:
        info = self.adapter.symbol_info(document["symbol"])
        if info is None:
            document["fault"] = "fill_protection_unavailable"
            return
        direction = Decimal(1 if side == "long" else -1)
        price = Decimal(leg["fill_price"])
        sl = self.signals._quantize_price(price - direction * request.stop_distance, info.digits)
        tp = self.signals._quantize_price(price + direction * request.target_distance, info.digits)
        for position in positions:
            if (
                not self._owned(position, document)
                or position.get("identifier", position.get("ticket")) not in leg["position_ids"]
            ):
                continue
            if (
                Decimal(str(position.get("sl", 0))) == sl
                and Decimal(str(position.get("tp", 0))) == tp
            ):
                leg["applied_protection"] = {
                    "stop_loss": str(sl),
                    "take_profit": str(tp),
                    "anchor": "actual_fill",
                    "confirmed": True,
                }
                continue
            change = {
                "action": self.adapter.constants.trade_action_sltp,
                "symbol": document["symbol"],
                "position": position["ticket"],
                "sl": float(sl),
                "tp": float(tp),
            }
            leg["protection_intent"] = change
            self.repository.save(document)
            try:
                check = self.adapter.order_check(change)
                if check is None or check.get("retcode") != 0:
                    document["fault"] = "fill_protection_rejected"
                    continue
                result = self.adapter.order_send(change)
                leg["protection_result"] = result
                if result is None or result.get("retcode") != self.adapter.constants.retcode_done:
                    document["fault"] = "fill_protection_unknown"
                else:
                    # Confirmation comes from the next inventory scan, not this request result.
                    leg["applied_protection"] = {
                        "stop_loss": str(sl),
                        "take_profit": str(tp),
                        "anchor": "actual_fill",
                        "confirmed": False,
                    }
            except Exception as exc:
                document["fault"] = "fill_protection_unknown"
                leg["protection_result"] = {"unknown": type(exc).__name__}

    def _close_loser(
        self,
        document: dict[str, Any],
        leg: dict[str, Any],
        positions: list[dict[str, Any]],
    ) -> None:
        for position in positions:
            if (
                not self._owned(position, document)
                or position.get("identifier", position.get("ticket")) not in leg["position_ids"]
            ):
                continue
            ticket = str(position["ticket"])
            previous = leg.setdefault("compensations", {}).get(ticket)
            if previous is not None and previous.get("unknown"):
                continue
            tick = self.adapter.symbol_tick(document["symbol"])
            info = self.adapter.symbol_info(document["symbol"])
            if tick is None or info is None:
                continue
            is_buy = position["type"] == self.adapter.constants.order_type_buy
            close = {
                "action": self.adapter.constants.trade_action_deal,
                "position": position["ticket"],
                "symbol": document["symbol"],
                "volume": position["volume"],
                "type": self.adapter.constants.order_type_sell
                if is_buy
                else self.adapter.constants.order_type_buy,
                "price": tick.bid if is_buy else tick.ask,
                "magic": self.settings.magic_number,
                "deviation": self.settings.default_deviation_points,
                "type_filling": self.signals._filling_policy(info),
                "comment": "oco-recovery",
            }
            leg["compensations"][ticket] = {"intent": close, "unknown": True}
            self.repository.save(document)
            try:
                result = self.adapter.order_send(close)
                leg["compensations"][ticket].update(
                    result=result,
                    unknown=(
                        result is None
                        or result.get("retcode")
                        not in {
                            self.adapter.constants.retcode_done,
                            self.adapter.constants.retcode_done_partial,
                        }
                    ),
                )
            except Exception as exc:
                leg["compensations"][ticket]["reason"] = type(exc).__name__
            self.repository.save(document)

    async def monitor_once(self, *, startup: bool = False) -> None:
        async with self.signals._terminal_lock:
            await asyncio.to_thread(self._monitor, startup)

    def _monitor(self, startup: bool) -> None:
        try:
            # Reconcile already-owned exposure even when new OCO entries are disabled.
            connection = self.adapter.connection_snapshot()
            if not self.signals._connection_ready(connection):
                raise RuntimeError("MT5 connection unavailable")
            groups = self.repository.all()
            if groups:
                account = self.adapter.account_metadata()
                if account.get("margin_mode") != 2 or account.get("login") != self.settings.login:
                    raise RuntimeError("MT5 OCO account changed")
            tags = {leg["broker_tag"] for group in groups for leg in group["legs"].values()}
            orphans = [
                row
                for row in self.adapter.active_orders()
                if row.get("magic") == self.settings.magic_number
                and str(row.get("comment", "")).startswith("oco-")
                and row.get("comment") not in tags
            ]
            if orphans:
                raise RuntimeError("owned OCO orders missing from durable ledger")
            for document in groups:
                self._verify_account(document)
                if document["state"] == "rejected":
                    continue
                if startup and (
                    not document.get("placement_complete", False) or self._uncertain(document)
                ):
                    document["cancel_requested"] = True
                    document["cancel_reason"] = "interrupted_placement"
                    self.repository.save(document)
                self._poll_group(document, *self._inventory(document))
            self.monitor_error = None
            self.recovered = True
            self.last_scan = time.monotonic()
        except Exception as exc:
            self.monitor_error = type(exc).__name__ + ": " + str(exc)[:120]

    async def run(self) -> None:
        while True:
            await self.monitor_once()
            await asyncio.sleep(self.settings.mt5_oco_poll_seconds)

    async def inventory(self) -> dict[str, Any]:
        async with self.signals._terminal_lock:
            orders, positions = await asyncio.to_thread(
                lambda: (self.adapter.active_orders(), self.adapter.active_positions())
            )
        return {"profile": self.settings.profile, "orders": orders, "positions": positions}

    async def close_owned_group(self, group_id: UUID) -> dict[str, Any]:
        """Explicit operator cleanup, restricted to this group's owned hedge positions."""
        async with self.signals._terminal_lock:
            document = await self.get(group_id)

            def close() -> None:
                self._verify_account(document)
                document["cancel_requested"] = True
                document["cancel_reason"] = document.get("cancel_reason") or "operator_cleanup"
                self.repository.save(document)
                inventory = self._inventory(document)
                self._poll_group(document, *inventory)
                current_positions = self._inventory(document)[1]
                for leg in document["legs"].values():
                    if leg["state"] != "closed":
                        self._close_loser(document, leg, current_positions)
                self.repository.save(document)

            await asyncio.to_thread(close)
            return document

    async def acknowledge_recovery(self, group_id: UUID) -> dict[str, Any]:
        """Clear an incident only after live state and history prove the group settled."""
        async with self.signals._terminal_lock:
            document = await self.get(group_id)
            await asyncio.to_thread(self._verify_account, document)
            await asyncio.to_thread(
                self._poll_group, document, *await asyncio.to_thread(self._inventory, document)
            )
            if any(
                leg["state"] not in TERMINAL_LEGS or leg.get("resting") is not False
                for leg in document["legs"].values()
            ):
                raise ServiceError(
                    409, "oco_recovery_incomplete", "Owned group exposure is not settled"
                )
            document["acknowledged_fault"] = document.get("fault")
            document["fault"] = None
            document["state"] = "closed"
            await asyncio.to_thread(self.repository.save, document)
            return document
