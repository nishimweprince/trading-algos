"""Client for the execution hub (services/execution-service on 127.0.0.1:8010).

The hub is the only cTrader credential holder. Everything here goes over HTTP with the hub's API key:
quotes, M1 candles, positions (with cTrader labels), deals, market orders and closes.

Order rules the hub enforces (see its README): `occurred_at` must be < SIGNAL_MAX_AGE_SECONDS old, so an operation
is never resent later with the same id (poll `/v1/operations/{id}` instead); a 202 / unknown outcome may still fill,
so the caller reconciles by label rather than resubmitting; SL/TP are sent as price distances (Decimal strings, so
the hub's `int(distance * 100000)` cannot truncate a binary float).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx


@dataclass
class OrderOutcome:
    operation_id: str
    state: str  # succeeded | rejected | pending | unknown | partial_failure
    position_id: int | None = None
    price: float | None = None
    volume: float | None = None
    error: str | None = None
    raw: dict | None = None


def decimal_text(x: float, digits: int) -> str:
    return format(Decimal(repr(round(x, digits))).quantize(Decimal(1).scaleb(-digits)), "f")


class Hub:
    def __init__(self, url: str, api_key: str, account: str, timeout: float = 30.0,
                 client: httpx.Client | None = None):
        self.account = account
        self.client = client or httpx.Client(base_url=url.rstrip("/"), timeout=timeout,
                                             headers={"X-API-Key": api_key})

    # ------------------------------------------------------------------ reads
    def _get(self, path: str, **params) -> Any:
        r = self.client.get(path, params={k: v for k, v in params.items() if v is not None})
        r.raise_for_status()
        return r.json()

    def accounts(self) -> list[dict]:
        return self._get("/v1/accounts")

    def quote(self, instrument: str) -> dict | None:
        try:
            return self._get(f"/v1/accounts/{self.account}/quote", instrument=instrument)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    def candles(self, instrument: str, count: int, to: datetime | None = None) -> list[dict]:
        return self._get(f"/v1/accounts/{self.account}/candles", instrument=instrument, timeframe="M1",
                         count=count, to=to.isoformat() if to else None)

    def history(self, instrument: str, since: datetime, page: int = 5000) -> list[dict]:
        """All closed M1 candles with interval end > since, oldest first (pages backwards)."""
        out: dict[str, dict] = {}
        to: datetime | None = None
        while True:
            got = self.candles(instrument, page, to)
            if not got:
                break
            for c in got:
                out[c["ts"]] = c
            first = datetime.fromisoformat(got[0]["ts"])
            if first <= since or len(got) < page // 2:
                break
            to = first
        rows = sorted(out.values(), key=lambda c: c["ts"])
        return [c for c in rows if datetime.fromisoformat(c["ts"]) > since]

    def positions(self) -> list[dict]:
        return self._get(f"/v1/accounts/{self.account}/positions")

    def deals(self, since: datetime, until: datetime | None = None) -> list[dict]:
        return self._get(f"/v1/accounts/{self.account}/deals", since=since.isoformat(),
                         until=until.isoformat() if until else None)

    def operation(self, operation_id: str) -> dict | None:
        r = self.client.get(f"/v1/operations/{operation_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    # ----------------------------------------------------------------- writes
    def _post(self, path: str, body: dict, operation_id: str) -> OrderOutcome:
        try:
            r = self.client.post(path, json=body)
        except httpx.HTTPError as exc:
            return OrderOutcome(operation_id, "unknown", error=f"transport: {exc}")
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code in (409, 422):
            err = (data.get("error") or {}) if isinstance(data, dict) else {}
            return OrderOutcome(operation_id, "rejected", error=f"{err.get('code')}: {err.get('message')}", raw=data)
        if r.status_code >= 400:
            return OrderOutcome(operation_id, "unknown", error=f"http {r.status_code}: {r.text[:200]}", raw=data)
        return self._outcome(operation_id, data)

    @staticmethod
    def _outcome(operation_id: str, data: dict) -> OrderOutcome:
        state = data.get("state", "unknown")
        tgt = (data.get("targets") or [{}])[0]
        price = tgt.get("execution_price")
        vol = tgt.get("executed_volume_lots")
        err = tgt.get("error_code")
        return OrderOutcome(operation_id, state, tgt.get("position_id"), float(price) if price else None,
                            float(vol) if vol else None,
                            f"{err}: {tgt.get('error_message')}" if err else None, data)

    def wait(self, outcome: OrderOutcome, seconds: float = 30.0) -> OrderOutcome:
        """Poll a pending/unknown operation until it settles or `seconds` pass."""
        deadline = time.monotonic() + seconds
        while outcome.state in ("pending", "unknown") and time.monotonic() < deadline:
            time.sleep(1.0)
            try:
                data = self.operation(outcome.operation_id)
            except httpx.HTTPError:
                continue
            if data is not None:
                outcome = self._outcome(outcome.operation_id, data)
        return outcome

    def market_order(self, *, source: str, instrument: str, side: int, lots: float, sl_distance: float,
                     tp_distance: float, digits: int, note: str | None = None) -> OrderOutcome:
        op = str(uuid.uuid4())
        body = {
            "operation_id": op,
            "occurred_at": datetime.now(UTC).isoformat(),
            "source": source,
            "instrument": instrument,
            "execution_type": "market",
            "direction": "buy" if side == 1 else "sell",
            "targets": [{"account": self.account, "volume_lots": format(Decimal(str(lots)), "f")}],
            "stop_loss_distance": decimal_text(sl_distance, digits),
            "take_profit_distance": decimal_text(tp_distance, digits),
            "note": note or source,
        }
        return self._post("/v1/orders", body, op)

    def close_position(self, *, source: str, position_id: int, lots: float) -> OrderOutcome:
        op = str(uuid.uuid4())
        body = {
            "operation_id": op,
            "occurred_at": datetime.now(UTC).isoformat(),
            "source": source,
            "targets": [{"account": self.account, "position_id": int(position_id),
                         "volume_lots": format(Decimal(str(lots)), "f")}],
        }
        return self._post("/v1/positions/close", body, op)
