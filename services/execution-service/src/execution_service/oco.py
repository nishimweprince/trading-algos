"""Gateway-owned OCO coordination; no candle loop participates in cancellation.

A group is a buy-stop above and a sell-stop below the market; the first fill
wins and its sibling is cancelled. The coordinator here owns everything
generic about that: the durable document, idempotency, admission gates, the
leg and group state machines, winner selection, expiry, faults and incident
acknowledgement. Each account's broker supplies an ``OcoVenue``
(``ta_plugin_api.oco``) for the broker side: the leg orders it sends and the
facts it reads back from its own inventory. MetaTrader 5 is the only venue;
a cTrader account answers 501 ``oco_not_supported``.

Entries are never resent after dispatch begins. Cancel and protection
operations are reconciled against live state.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

from ta_contracts import OcoGroupRequest, SignalRequest
from ta_core import ServiceError
from ta_plugin_api.oco import OcoObservation, OcoVenue
from ta_store import OcoGroupStore

from .config import Settings
from .signals import validate_signal_freshness, validate_signal_source

__all__ = ["OcoCoordinator", "OcoRouter"]

TERMINAL_LEGS = {"not_submitted", "cancelled", "expired", "rejected", "closed"}
UNCERTAIN_LEGS = {"dispatching", "unknown"}
SCAN_FRESHNESS_SECONDS = 10


class OcoCoordinator:
    """OCO groups on one account, through that account's venue."""

    def __init__(self, settings: Settings, venue: OcoVenue, repository: OcoGroupStore) -> None:
        self.settings = settings
        self.venue = venue
        self.account = venue.account
        self.repository = repository
        self.recovered = False
        self.monitor_error: str | None = "startup reconciliation pending"
        self.last_scan: float | None = None

    # --- reads ------------------------------------------------------------------

    def groups(self) -> list[dict[str, Any]]:
        return self.repository.all(self.account)

    def _save(self, document: dict[str, Any]) -> None:
        self.repository.save(document)

    async def get(self, group_id: UUID | str) -> dict[str, Any]:
        record = await asyncio.to_thread(self.repository.get, str(group_id))
        if record is None:
            raise ServiceError(404, "oco_not_found", "No OCO group has this ID")
        return record[1]

    @staticmethod
    def _uncertain(group: dict[str, Any]) -> bool:
        return any(leg["state"] in UNCERTAIN_LEGS for leg in group["legs"].values())

    def _guard(self, symbol: str | None = None) -> None:
        if not self.settings.mt5_oco_enabled:
            raise ServiceError(503, "oco_disabled", "MT5_OCO_ENABLED is false")
        self.venue.guard(symbol)

    async def capabilities(self, symbol: str | None = None) -> dict[str, Any]:
        reason = "ready"
        ready = False
        try:
            async with self.venue.exclusive():
                await asyncio.to_thread(self._guard, symbol)
                groups = await asyncio.to_thread(self.groups)
            fresh = (
                self.last_scan is not None
                and time.monotonic() - self.last_scan < SCAN_FRESHNESS_SECONDS
            )
            ready = self.recovered and fresh and self.monitor_error is None
            if any(group.get("fault") or self._uncertain(group) for group in groups):
                ready = False
                reason = "owned OCO exposure needs reconciliation"
            elif not ready:
                reason = self.monitor_error or "OCO monitor is not current"
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            reason = exc.code if isinstance(exc, ServiceError) else type(exc).__name__
        return {
            "version": 1,
            "profile": self.settings.profile,
            "account": self.account,
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

    async def inventory(self) -> dict[str, Any]:
        async with self.venue.exclusive():
            return await asyncio.to_thread(self.venue.inventory)

    # --- submission -------------------------------------------------------------

    async def submit(self, request: OcoGroupRequest) -> dict[str, Any]:
        async with self.venue.exclusive():
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
        if request.profile != self.account:
            raise ServiceError(
                422, "oco_profile_mismatch", "Request targets a different MT5 profile"
            )
        if not self.recovered or self.monitor_error is not None:
            raise ServiceError(
                503, "oco_not_reconciled", "OCO monitor has not reconciled broker state"
            )
        if self.last_scan is None or time.monotonic() - self.last_scan >= SCAN_FRESHNESS_SECONDS:
            raise ServiceError(503, "oco_monitor_stale", "OCO monitor has not scanned recently")
        if any(group.get("fault") or self._uncertain(group) for group in self.groups()):
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
        document.update(self.venue.account_identity())
        document["legs"] = {
            side: {
                "state": "not_submitted",
                "signal_id": str(uuid5(request.group_id, side)),
                "broker_tag": self.venue.leg_tag(request.group_id, side),
                "order_id": None,
                "fill_price": None,
                "executed_volume": "0",
                "position_ids": [],
                "applied_protection": {},
            }
            for side in ("long", "short")
        }
        if not self.repository.reserve(group_id, self.account, payload_hash, document):
            raise ServiceError(409, "oco_in_progress", "OCO group is being processed")
        prepared: list[tuple[str, dict[str, Any]]] = []
        try:
            for side in ("long", "short"):
                leg = document["legs"][side]
                signal = self._signal(request, side)
                validate_signal_source(self.settings, signal)
                validate_signal_freshness(self.settings, signal)
                validate_signal_freshness(
                    self.settings, signal.model_copy(update={"occurred_at": request.decision_at})
                )
                order, protection = self.venue.prepare_leg(
                    request, side, leg["broker_tag"], document["server_utc_offset_seconds"]
                )
                leg["request"] = order
                leg["applied_protection"] = protection
                prepared.append((side, order))
        except Exception as exc:  # noqa: BLE001 - recorded on the group
            document["state"] = "rejected"
            document["reason"] = exc.code if isinstance(exc, ServiceError) else type(exc).__name__
            for leg in document["legs"].values():
                leg["state"] = "rejected"
            self._save(document)
            return document
        self._save(document)
        for side, order in prepared:
            leg = document["legs"][side]
            if document["winner"] is not None or document["cancel_requested"]:
                break
            leg["state"] = "dispatching"
            self._save(document)
            sent = self.venue.send_leg(order)
            if sent.error is not None:
                leg["reason"] = sent.error
            leg["result"] = sent.result
            if sent.result is None:
                leg["state"] = "unknown"
                document["cancel_requested"] = True
                document["cancel_reason"] = "incomplete_placement"
            elif sent.order_id is not None:
                leg["order_id"] = sent.order_id
                leg["state"] = "placed"
            else:
                leg["state"] = "rejected"
                document["cancel_requested"] = True
                document["cancel_reason"] = "incomplete_placement"
            self._save(document)
            try:
                self._poll_group(document, self.venue.observe(document))
            except Exception as exc:  # noqa: BLE001 - the monitor reports it
                document["cancel_requested"] = True
                document["cancel_reason"] = "inventory_unavailable"
                self.monitor_error = type(exc).__name__
                self._save(document)
                break
        document["placement_complete"] = True
        self._save(document)
        return document

    @staticmethod
    def _signal(request: OcoGroupRequest, side: str) -> SignalRequest:
        """A leg as a signal, so it passes the same source and age policy."""
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

    # --- the state machine ------------------------------------------------------

    def _poll_group(self, document: dict[str, Any], observation: OcoObservation) -> None:
        request = OcoGroupRequest.model_validate(
            {key: document[key] for key in OcoGroupRequest.model_fields}
        )
        for _side, leg in document["legs"].items():
            matching = observation.tagged_orders(leg["broker_tag"])
            if leg["order_id"] is None and matching:
                if len(matching) != 1:
                    document["fault"] = "duplicate_owned_leg"
                    continue
                leg["order_id"] = matching.pop()
            ticket = leg["order_id"]
            if ticket is None:
                leg["resting"] = None if leg["state"] in UNCERTAIN_LEGS else False
                continue
            fills = observation.fills(ticket)
            live = observation.is_live(ticket)
            terminal = observation.terminal_state(ticket)
            if fills is not None:
                volume = fills.executed_volume
                leg.update(
                    executed_volume=str(volume),
                    fill_price=str(fills.fill_price),
                    position_ids=fills.position_ids,
                    filled_at_msc=fills.filled_at_msc,
                    state="partially_filled" if volume < request.volume else "filled",
                )
                leg["accounting"] = fills.accounting
                if not fills.open_positions and not live and fills.exit_volume >= volume:
                    leg["state"] = "closed"
            elif live:
                leg["state"] = "placed"
            elif terminal is not None:
                leg["state"] = terminal
            elif leg["state"] not in TERMINAL_LEGS:
                leg["state"] = "unknown"
            leg["resting"] = None if leg["state"] in UNCERTAIN_LEGS else live
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
        self._save(document)
        for side, leg in document["legs"].items():
            live = leg["order_id"] is not None and observation.is_live(leg["order_id"])
            should_cancel = document["cancel_requested"] or document["winner"] is not None
            if live and should_cancel:
                self.venue.cancel_leg(document, leg, leg["order_id"], self._save)
            if Decimal(leg["executed_volume"]) > 0 and leg["state"] != "closed":
                if document.get("fault") == "both_legs_filled" and side != document["winner"]:
                    self.venue.close_positions(document, leg, observation, self._save)
                else:
                    self.venue.protect_fill(document, side, leg, request, observation, self._save)
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
        self._save(document)

    # --- control ----------------------------------------------------------------

    async def cancel(self, group_id: UUID | str, reason: str) -> dict[str, Any]:
        async with self.venue.exclusive():
            document = await self.get(group_id)
            await asyncio.to_thread(self.venue.verify_account, document)
            document["cancel_requested"] = True
            document["cancel_reason"] = reason
            await asyncio.to_thread(self._save, document)
            observation = await asyncio.to_thread(self.venue.observe, document)
            await asyncio.to_thread(self._poll_group, document, observation)
            return document

    async def close_owned_group(self, group_id: UUID | str) -> dict[str, Any]:
        """Explicit operator cleanup, restricted to this group's owned positions."""
        async with self.venue.exclusive():
            document = await self.get(group_id)

            def close() -> None:
                self.venue.verify_account(document)
                document["cancel_requested"] = True
                document["cancel_reason"] = document.get("cancel_reason") or "operator_cleanup"
                self._save(document)
                self._poll_group(document, self.venue.observe(document))
                current = self.venue.observe(document)
                for leg in document["legs"].values():
                    if leg["state"] != "closed":
                        self.venue.close_positions(document, leg, current, self._save)
                self._save(document)

            await asyncio.to_thread(close)
            return document

    async def acknowledge_recovery(self, group_id: UUID | str) -> dict[str, Any]:
        """Clear an incident only after live state and history prove the group settled."""
        async with self.venue.exclusive():
            document = await self.get(group_id)
            await asyncio.to_thread(self.venue.verify_account, document)
            observation = await asyncio.to_thread(self.venue.observe, document)
            await asyncio.to_thread(self._poll_group, document, observation)
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
            await asyncio.to_thread(self._save, document)
            return document

    # --- the monitor ------------------------------------------------------------

    async def monitor_once(self, *, startup: bool = False) -> None:
        async with self.venue.exclusive():
            await asyncio.to_thread(self._monitor, startup)

    def _monitor(self, startup: bool) -> None:
        try:
            # Reconcile already-owned exposure even when new OCO entries are disabled.
            groups = self.groups()
            tags = {leg["broker_tag"] for group in groups for leg in group["legs"].values()}
            self.venue.monitor_preflight(bool(groups), tags)
            for document in groups:
                self.venue.verify_account(document)
                if document["state"] == "rejected":
                    continue
                if startup and (
                    not document.get("placement_complete", False) or self._uncertain(document)
                ):
                    document["cancel_requested"] = True
                    document["cancel_reason"] = "interrupted_placement"
                    self._save(document)
                self._poll_group(document, self.venue.observe(document))
            self.monitor_error = None
            self.recovered = True
            self.last_scan = time.monotonic()
        except Exception as exc:  # noqa: BLE001 - surfaced through capabilities
            self.monitor_error = type(exc).__name__ + ": " + str(exc)[:120]

    async def run(self) -> None:
        while True:
            await self.monitor_once()
            await asyncio.sleep(self.settings.mt5_oco_poll_seconds)


class OcoRouter:
    """Finds the coordinator for an account or a stored group.

    ``unsupported`` holds accounts this host serves through a provider with no
    OCO venue (cTrader), which answer 501 rather than "unknown account".
    """

    def __init__(
        self,
        coordinators: dict[str, OcoCoordinator],
        unsupported: set[str],
        repository: OcoGroupStore,
    ) -> None:
        self.coordinators = coordinators
        self.unsupported = unsupported
        self.repository = repository
        self._tasks: list[asyncio.Task[None]] = []

    def for_account(self, account: str | None) -> OcoCoordinator:
        if account is None:
            if len(self.coordinators) == 1:
                return next(iter(self.coordinators.values()))
            if not self.coordinators:
                raise ServiceError(
                    501, "oco_not_supported", "No account on this host supports OCO groups"
                )
            raise ServiceError(422, "account_required", "Name the account to query")
        coordinator = self.coordinators.get(account)
        if coordinator is not None:
            return coordinator
        if account in self.unsupported:
            raise ServiceError(
                501, "oco_not_supported", f"Account {account} does not support OCO groups"
            )
        raise ServiceError(
            422, "oco_profile_mismatch", "Request targets an account this host does not serve"
        )

    async def for_group(self, group_id: UUID | str) -> OcoCoordinator:
        account = await asyncio.to_thread(self.repository.account_of, str(group_id))
        if account is None:
            raise ServiceError(404, "oco_not_found", "No OCO group has this ID")
        return self.for_account(account)

    async def submit(self, request: OcoGroupRequest) -> dict[str, Any]:
        """A replayed group goes to the account it was stored on, so a replay
        answers from the ledger even if the request names another account."""
        stored = await asyncio.to_thread(self.repository.account_of, str(request.group_id))
        return await self.for_account(stored or request.profile).submit(request)

    async def start(self, accounts: list[str]) -> None:
        """Reconcile, then monitor, the given accounts' groups.

        A monitor runs when OCO is enabled or the account still holds groups:
        owned exposure is reconciled even after new entries are switched off.
        """
        for account in accounts:
            coordinator = self.coordinators.get(account)
            if coordinator is None:
                continue
            has_groups = bool(await asyncio.to_thread(coordinator.groups))
            if coordinator.settings.mt5_oco_enabled or has_groups:
                await coordinator.monitor_once(startup=True)
                self._tasks.append(asyncio.create_task(coordinator.run()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
