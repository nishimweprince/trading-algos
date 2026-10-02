"""A stateful stand-in for execution-service's HTTP surface (httpx.MockTransport)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

ACCOUNT = "binance_testnet"


@dataclass
class FakeQuote:
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


class FakeQuotes:
    def __init__(self, bid: float = 60000.0, ask: float = 60000.1) -> None:
        self.quotes = {"BTCUSDT": FakeQuote(bid, ask), "ETHUSDT": FakeQuote(3000.0, 3000.01)}
        self.started = False

    def get(self, symbol: str, max_age_ms: float) -> FakeQuote | None:
        return self.quotes.get(symbol)

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.started = False

    def status(self) -> dict[str, Any]:
        return {"connected": True}


@dataclass
class FakeGateway:
    positions: list[dict[str, Any]] = field(default_factory=list)
    ready: bool = True
    wallet: str = "1000"
    # POST /v1/orders: "ok" | "drop" (arrives, response lost) | "down" (never arrives)
    submit_mode: str = "ok"
    operations: dict[str, dict[str, Any]] = field(default_factory=dict)
    payloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[str, str, Any]] = field(default_factory=list)
    next_order_id: int = 1000

    # --- test controls -----------------------------------------------------------

    def orders(self) -> list[dict[str, Any]]:
        return list(self.payloads.values())

    def op_for(self, leg_suffix: str) -> str:
        """The operation id of the (single) order whose note ends with ``|<leg>``."""
        (op,) = [
            op for op, p in self.payloads.items() if p.get("note", "").endswith(f"|{leg_suffix}")
        ]
        return op

    def settle(
        self, operation_id: str, state: str, qty: str | None = None, price: str | None = None
    ) -> None:
        target = self.operations[operation_id]["targets"][0]
        target["state"] = state
        if qty is not None:
            target["executed_volume_lots"] = qty
        if price is not None:
            target["execution_price"] = price

    def paths(self) -> list[str]:
        return [path for _, path, _ in self.calls]

    # --- the transport -------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        now = datetime.now(UTC).isoformat()
        if path == "/health/trading-ready":
            if self.ready:
                return httpx.Response(200, json={"status": "ready"})
            return httpx.Response(503, json={"status": "not_ready", "reason": "preflight"})
        if path == "/v1/accounts" and request.method == "GET":
            return httpx.Response(
                200, json={"accounts": [{"alias": ACCOUNT, "wallet_balance_usdt": self.wallet}]}
            )
        if path == f"/v1/accounts/{ACCOUNT}/positions":
            return httpx.Response(200, json=self.positions)
        if path.startswith(f"/v1/accounts/{ACCOUNT}/"):
            return httpx.Response(200, json={"ok": True, "details": {}})
        if path == "/v1/orders":
            if self.submit_mode == "down":
                raise httpx.ConnectError("gateway down")
            op = body["operation_id"]
            self.payloads[op] = body
            self.next_order_id += 1
            state = "accepted" if body["execution_type"] == "market" else "placed"
            self.operations[op] = {
                "operation_id": op,
                "action": "place",
                "state": "pending",
                "targets": [
                    {
                        "account": ACCOUNT,
                        "state": state,
                        "order_id": self.next_order_id,
                        "updated_at": now,
                    }
                ],
                "created_at": now,
                "updated_at": now,
            }
            if self.submit_mode == "drop":
                raise httpx.ReadTimeout("response lost")
            return httpx.Response(201, json=self.operations[op])
        if path == "/v1/orders/cancel":
            order_id = body["targets"][0]["order_id"]
            for op in self.operations.values():
                target = op["targets"][0]
                if target.get("order_id") == order_id and target["state"] in {
                    "placed",
                    "partially_filled",
                }:
                    target["state"] = (
                        "partially_filled_final"
                        if target.get("executed_volume_lots")
                        else "cancelled"
                    )
            return httpx.Response(
                201,
                json={"operation_id": body["operation_id"], "state": "succeeded", "targets": []},
            )
        if path.startswith("/v1/operations/"):
            op = path.rsplit("/", 1)[1]
            if op not in self.operations:
                return httpx.Response(404, json={"error": {"code": "not_found"}})
            return httpx.Response(200, json=self.operations[op])
        return httpx.Response(404, json={"error": {"code": "no_route"}})
