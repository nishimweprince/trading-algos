"""MetaTrader 5 as an OCO venue.

Moved verbatim out of execution-service's MT5 OCO service; the group state
machine stayed there, in the generic coordinator. What lives here is
everything that depends on how MT5 reports a trade:

- ownership is ``magic`` plus symbol, and a leg is found by its order comment;
- history order states 2/5/6 are cancelled/rejected/expired;
- deal ``entry`` 0 opens a position and 1/3 close one;
- deal times are broker-server time, so they are shifted back to UTC with the
  offset recorded on the group, and history is queried across both clocks;
- pending legs use RETURN filling and a server-time expiry;
- OCO needs a hedging account (``margin_mode`` 2): recovery must be able to
  close an owned losing position without reversing shared netting exposure.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from ta_contracts import Direction, ExecutionType, OcoGroupRequest
from ta_core import ServiceError
from ta_plugin_api.oco import FillFacts, LegSend, SaveFn

from .execution import MT5Execution, PlaceIntent, quantize_price

__all__ = ["MT5Oco", "MT5OcoObservation", "MT5OcoSettings"]

HEDGING = 2
SPECIFIED_EXPIRY = 4
HISTORY_TERMINAL_STATES = {2: "cancelled", 5: "rejected", 6: "expired"}


class MT5OcoSettings(Protocol):
    profile: str | None
    login: int | None
    server: str | None
    magic_number: int
    default_deviation_points: int
    mt5_oco_server_utc_offset_seconds: int


class MT5OcoObservation:
    """One inventory snapshot: live orders and positions, order and deal history."""

    def __init__(
        self,
        document: dict[str, Any],
        magic: int,
        orders: list[dict[str, Any]],
        positions: list[dict[str, Any]],
        history: list[dict[str, Any]],
        deals: list[dict[str, Any]],
    ) -> None:
        self.document = document
        self.magic = magic
        self.orders = orders
        self.positions = positions
        self.history = history
        self.deals = deals

    def owned(self, record: dict[str, Any]) -> bool:
        return record.get("magic") == self.magic and record.get("symbol") == self.document["symbol"]

    def tagged_orders(self, tag: str) -> set[int]:
        return {
            int(row["ticket"])
            for row in self.orders + self.history
            if self.owned(row) and row.get("comment") == tag
        }

    def is_live(self, order_id: int) -> bool:
        return any(row.get("ticket") == order_id and self.owned(row) for row in self.orders)

    def terminal_state(self, order_id: int) -> str | None:
        terminal = next(
            (row for row in self.history if row.get("ticket") == order_id and self.owned(row)),
            None,
        )
        if terminal is None:
            return None
        return HISTORY_TERMINAL_STATES.get(terminal.get("state"))  # type: ignore[arg-type]

    def owned_positions(self, position_ids: list[int]) -> list[dict[str, Any]]:
        return [
            row
            for row in self.positions
            if self.owned(row) and row.get("identifier", row.get("ticket")) in position_ids
        ]

    def fills(self, order_id: int) -> FillFacts | None:
        entry_deals = [
            row
            for row in self.deals
            if self.owned(row) and row.get("order") == order_id and row.get("entry") == 0
        ]
        if not entry_deals:
            return None
        volume = sum(Decimal(str(row["volume"])) for row in entry_deals)
        price = (
            sum(Decimal(str(row["price"])) * Decimal(str(row["volume"])) for row in entry_deals)
            / volume
        )
        position_ids = sorted({int(row["position_id"]) for row in entry_deals})
        filled_at_msc = (
            min(int(row.get("time_msc", row.get("time", 0) * 1000)) for row in entry_deals)
            - self.document.get("server_utc_offset_seconds", 0) * 1000
        )
        # Exits are not ownership-filtered: a manual close in the terminal
        # carries magic 0 but still closes this leg's position.
        exits = [
            row
            for row in self.deals
            if row.get("symbol") == self.document["symbol"]
            and row.get("position_id") in position_ids
            and row.get("entry") in {1, 3}
        ]
        accounting_deals = entry_deals + exits
        accounting = {
            key: str(sum(Decimal(str(row.get(key, 0))) for row in accounting_deals))
            for key in ("profit", "commission", "swap", "fee")
        }
        accounting["realized_net_pnl"] = str(
            sum(Decimal(accounting[key]) for key in ("profit", "commission", "swap", "fee"))
        )
        return FillFacts(
            executed_volume=volume,
            fill_price=price,
            position_ids=position_ids,
            filled_at_msc=filled_at_msc,
            accounting=accounting,
            exit_volume=sum((Decimal(str(row["volume"])) for row in exits), Decimal(0)),
            open_positions=bool(self.owned_positions(position_ids)),
        )


class MT5Oco:
    def __init__(self, execution: MT5Execution, settings: MT5OcoSettings) -> None:
        self.execution = execution
        self.adapter = execution.adapter
        self.settings = settings
        self.account = execution.account

    def exclusive(self) -> AbstractAsyncContextManager[None]:
        return self.execution.exclusive()

    # --- admission --------------------------------------------------------------

    def guard(self, symbol: str | None) -> None:
        self.execution.ensure_ready()
        account = self.adapter.account_metadata()
        if (
            account.get("login") != self.settings.login
            or account.get("margin_mode") != HEDGING
            or account.get("server") != self.settings.server
        ):
            raise ServiceError(
                422, "oco_account_unsupported", "OCO requires the configured hedge account"
            )
        if symbol is None:
            return
        info = self.adapter.symbol_info(symbol)
        if info is None or not info.expiration_mode & SPECIFIED_EXPIRY:
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

    def account_identity(self) -> dict[str, Any]:
        return {
            "account_login": self.settings.login,
            "account_server": self.settings.server,
            "account_currency": self.adapter.account_metadata().get("currency"),
            "server_utc_offset_seconds": self.settings.mt5_oco_server_utc_offset_seconds,
        }

    def verify_account(self, document: dict[str, Any]) -> None:
        account = self.adapter.account_metadata()
        if (
            account.get("login") != self.settings.login
            or account.get("margin_mode") != HEDGING
            or document.get("account_login") != account.get("login")
            or document.get("account_server") != account.get("server")
            or account.get("server") != self.settings.server
        ):
            raise ServiceError(
                409, "oco_account_changed", "Owned group belongs to a different account"
            )

    def monitor_preflight(self, has_groups: bool, owned_tags: set[str]) -> None:
        connection = self.adapter.connection_snapshot()
        if not self.execution.connection_ready(connection):
            raise RuntimeError("MT5 connection unavailable")
        if has_groups:
            account = self.adapter.account_metadata()
            if account.get("margin_mode") != HEDGING or account.get("login") != self.settings.login:
                raise RuntimeError("MT5 OCO account changed")
        orphans = [
            row
            for row in self.adapter.active_orders()
            if row.get("magic") == self.settings.magic_number
            and str(row.get("comment", "")).startswith("oco-")
            and row.get("comment") not in owned_tags
        ]
        if orphans:
            raise RuntimeError("owned OCO orders missing from durable ledger")

    # --- placement --------------------------------------------------------------

    def leg_tag(self, group_id: UUID, side: str) -> str:
        return f"oco-{group_id.hex[:20]}-{'b' if side == 'long' else 's'}"

    def prepare_leg(
        self, request: OcoGroupRequest, side: str, tag: str, server_offset: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        intent = PlaceIntent(
            symbol=request.symbol,
            direction=Direction.BUY if side == "long" else Direction.SELL,
            execution_type=ExecutionType.STOP,
            volume=request.volume,
            broker_tag=tag,
            entry_price=request.upper_trigger if side == "long" else request.lower_trigger,
            stop_loss_distance=request.stop_distance,
            take_profit_distance=request.target_distance,
            expires_at=request.expires_at,
            log_fields={"group_id": str(request.group_id), "side": side},
        )
        context = self.execution.symbol_context(intent)
        if context.adjustments:
            raise ServiceError(
                422, "oco_protection_widened", "Requested protection requires widening"
            )
        order = self.execution.build_request(intent, context)
        # MT5 pending orders use RETURN; filling flags govern later market execution.
        order["type_filling"] = self.adapter.constants.order_filling_return
        # The group/watchdog stays in UTC; MT5 pending expiry uses server time.
        order["expiration"] += server_offset
        check = self.adapter.order_check(order)
        if check is None or int(check.get("retcode", -1)) != 0:
            raise ServiceError(422, "oco_preflight_rejected", "Both OCO legs must pass preflight")
        protection = {
            "stop_loss": str(context.stop_loss),
            "take_profit": str(context.take_profit),
            "anchor": "pending_entry",
        }
        return order, protection

    def send_leg(self, order: dict[str, Any]) -> LegSend:
        try:
            result = self.adapter.order_send(order)
        except Exception as exc:  # noqa: BLE001 - the order may have reached the broker
            return LegSend(None, error=type(exc).__name__)
        if result is None:
            return LegSend(None)
        constants = self.adapter.constants
        accepted = {
            constants.retcode_placed,
            constants.retcode_done,
            constants.retcode_done_partial,
        }
        if int(result.get("retcode", -1)) in accepted and int(result.get("order", 0)) > 0:
            return LegSend(result, order_id=int(result["order"]))
        return LegSend(result)

    # --- observation and control ------------------------------------------------

    def observe(self, document: dict[str, Any]) -> MT5OcoObservation:
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
        return MT5OcoObservation(
            document,
            self.settings.magic_number,
            self.adapter.active_orders(),
            self.adapter.active_positions(),
            self.adapter.history_orders(start, end),
            self.adapter.history_deals(start, end),
        )

    def cancel_leg(
        self, document: dict[str, Any], leg: dict[str, Any], order_id: int, save: SaveFn
    ) -> None:
        leg["cancel_intent"] = {"order_id": order_id, "at": datetime.now(UTC).isoformat()}
        save(document)
        try:
            result = self.adapter.order_send(
                {
                    "action": self.adapter.constants.trade_action_remove,
                    "order": order_id,
                    "symbol": document["symbol"],
                }
            )
            leg["cancel_result"] = result
        except Exception as exc:  # noqa: BLE001 - recorded; the next scan confirms
            leg["cancel_result"] = {"unknown": type(exc).__name__}
        # Even DONE is an operation result; live inventory/history confirms cancellation.
        save(document)

    def protect_fill(
        self,
        document: dict[str, Any],
        side: str,
        leg: dict[str, Any],
        request: OcoGroupRequest,
        observation: MT5OcoObservation,  # type: ignore[override]
        save: SaveFn,
    ) -> None:
        info = self.adapter.symbol_info(document["symbol"])
        if info is None:
            document["fault"] = "fill_protection_unavailable"
            return
        direction = Decimal(1 if side == "long" else -1)
        price = Decimal(leg["fill_price"])
        sl = quantize_price(price - direction * request.stop_distance, info.digits)
        tp = quantize_price(price + direction * request.target_distance, info.digits)
        for position in observation.owned_positions(leg["position_ids"]):
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
            save(document)
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
            except Exception as exc:  # noqa: BLE001 - recorded as a fault
                document["fault"] = "fill_protection_unknown"
                leg["protection_result"] = {"unknown": type(exc).__name__}

    def close_positions(
        self,
        document: dict[str, Any],
        leg: dict[str, Any],
        observation: MT5OcoObservation,  # type: ignore[override]
        save: SaveFn,
    ) -> None:
        constants = self.adapter.constants
        for position in observation.owned_positions(leg["position_ids"]):
            ticket = str(position["ticket"])
            previous = leg.setdefault("compensations", {}).get(ticket)
            if previous is not None and previous.get("unknown"):
                continue
            tick = self.adapter.symbol_tick(document["symbol"])
            info = self.adapter.symbol_info(document["symbol"])
            if tick is None or info is None:
                continue
            is_buy = position["type"] == constants.order_type_buy
            close = {
                "action": constants.trade_action_deal,
                "position": position["ticket"],
                "symbol": document["symbol"],
                "volume": position["volume"],
                "type": constants.order_type_sell if is_buy else constants.order_type_buy,
                "price": tick.bid if is_buy else tick.ask,
                "magic": self.settings.magic_number,
                "deviation": self.settings.default_deviation_points,
                "type_filling": self.execution.filling_policy(info),
                "comment": "oco-recovery",
            }
            leg["compensations"][ticket] = {"intent": close, "unknown": True}
            save(document)
            try:
                result = self.adapter.order_send(close)
                leg["compensations"][ticket].update(
                    result=result,
                    unknown=(
                        result is None
                        or result.get("retcode")
                        not in {constants.retcode_done, constants.retcode_done_partial}
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - stays unknown; never retried blindly
                leg["compensations"][ticket]["reason"] = type(exc).__name__
            save(document)

    def inventory(self) -> dict[str, Any]:
        return {
            "profile": self.settings.profile,
            "orders": self.adapter.active_orders(),
            "positions": self.adapter.active_positions(),
        }
