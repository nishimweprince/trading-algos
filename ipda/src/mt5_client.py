"""Client for the mt5-trader signal-execution service."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx

from .config import Settings


@dataclass(slots=True)
class SubmitOutcome:
    kind: str  # success | rejected | unready | in_progress | unknown | unauthorized | error
    status_code: int | None = None
    detail: dict[str, Any] | None = None


def _retry_delay_seconds(attempt: int, base_ms: int) -> float:
    jitter = random.uniform(0, base_ms)
    return (base_ms * (2 ** max(0, attempt - 1)) + jitter) / 1000.0


class Mt5TraderClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._s = settings
        self._client = client
        self._base = settings.mt5_signal_api_url.rstrip("/")
        self._headers = {
            "Content-Type": "application/json",
            "X-API-Key": settings.mt5_signal_api_key.get_secret_value(),
        }

    async def is_ready(self) -> bool:
        try:
            resp = await self._client.get(
                f"{self._base}/health/ready", timeout=self._s.mt5_timeout_seconds
            )
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    async def submit(self, payload: dict[str, Any]) -> SubmitOutcome:
        if self._s.require_ready and not await self.is_ready():
            return SubmitOutcome(kind="unready")

        signal_id = UUID(str(payload["signal_id"]))
        last_error: SubmitOutcome | None = None

        for attempt in range(1, self._s.max_retries + 2):
            try:
                resp = await self._client.post(
                    f"{self._base}/v1/signals",
                    headers=self._headers,
                    content=_json_dumps(payload),
                    timeout=self._s.mt5_timeout_seconds,
                )
            except httpx.HTTPError as exc:
                reconciled = await self._reconcile(signal_id)
                if reconciled is not None:
                    return reconciled
                last_error = SubmitOutcome(kind="error", detail={"transport_error": str(exc)})
                await asyncio.sleep(_retry_delay_seconds(attempt, self._s.retry_base_delay_ms))
                continue

            if resp.status_code == 200:
                return SubmitOutcome(kind="success", status_code=200, detail=_safe_json(resp))
            if resp.status_code == 401:
                return SubmitOutcome(kind="unauthorized", status_code=401, detail=_safe_json(resp))
            if resp.status_code == 422:
                return SubmitOutcome(kind="rejected", status_code=422, detail=_safe_json(resp))
            if resp.status_code == 409:
                code = _error_code(resp)
                if code == "signal_in_progress":
                    reconciled = await self._reconcile(signal_id)
                    return reconciled or SubmitOutcome(
                        kind="in_progress", status_code=409, detail=_safe_json(resp)
                    )
                return SubmitOutcome(kind="error", status_code=409, detail=_safe_json(resp))
            if resp.status_code == 503:
                code = _error_code(resp)
                if code == "execution_outcome_unknown":
                    return SubmitOutcome(kind="unknown", status_code=503, detail=_safe_json(resp))
                last_error = SubmitOutcome(kind="unready", status_code=503, detail=_safe_json(resp))
                await asyncio.sleep(_retry_delay_seconds(attempt, self._s.retry_base_delay_ms))
                continue

            last_error = SubmitOutcome(
                kind="error", status_code=resp.status_code, detail=_safe_json(resp)
            )
            await asyncio.sleep(_retry_delay_seconds(attempt, self._s.retry_base_delay_ms))

        return last_error or SubmitOutcome(kind="error")

    async def _reconcile(self, signal_id: UUID) -> SubmitOutcome | None:
        try:
            resp = await self._client.get(
                f"{self._base}/v1/signals/{signal_id}", timeout=self._s.mt5_timeout_seconds
            )
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        body = _safe_json(resp) or {}
        state = body.get("state")
        if state in {"filled", "partially_filled", "placed"}:
            return SubmitOutcome(kind="success", status_code=200, detail=body)
        if state == "rejected":
            return SubmitOutcome(kind="rejected", status_code=200, detail=body)
        if state == "unknown":
            return SubmitOutcome(kind="unknown", status_code=200, detail=body)
        return None


def _json_dumps(payload: dict[str, Any]) -> bytes:
    import json

    def default(o: Any) -> Any:
        if isinstance(o, Decimal):
            return str(o)
        return str(o)

    return json.dumps(payload, default=default).encode("utf-8")


def _safe_json(resp: httpx.Response) -> dict[str, Any] | None:
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else {"body": data}


def _error_code(resp: httpx.Response) -> str | None:
    body = _safe_json(resp) or {}
    error = body.get("error")
    if isinstance(error, dict):
        return error.get("code")
    return None
