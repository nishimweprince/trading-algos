"""HTTP routes for OCO groups.

``/v1/oco*`` is the surface. MT5 hosts also keep the paths backtesting-service's
bridge already calls, ``/v1/mt5/oco*``, ``/v1/mt5/capabilities`` and
``/v1/mt5/inventory``, as aliases of the same handlers. See docs/oco.md.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, Query
from ta_contracts import OcoGroupRequest

from .oco import OcoRouter


def register_oco_routes(
    app: FastAPI, router: OcoRouter, authenticate: Any, *, mt5_aliases: bool
) -> None:
    auth = [Depends(authenticate)]

    async def capabilities(
        account: str | None = Query(default=None, max_length=63),
        symbol: str | None = Query(default=None, max_length=64),
    ) -> dict[str, Any]:
        return await router.for_account(account).capabilities(symbol)

    async def inventory(
        account: str | None = Query(default=None, max_length=63),
    ) -> dict[str, Any]:
        return await router.for_account(account).inventory()

    async def submit(request: OcoGroupRequest) -> dict[str, Any]:
        return await router.submit(request)

    async def get(group_id: UUID) -> dict[str, Any]:
        return await (await router.for_group(group_id)).get(group_id)

    async def cancel(
        group_id: UUID,
        reason: str = Query(default="engine_expiry", min_length=1, max_length=120),
    ) -> dict[str, Any]:
        return await (await router.for_group(group_id)).cancel(group_id, reason)

    async def close(group_id: UUID) -> dict[str, Any]:
        return await (await router.for_group(group_id)).close_owned_group(group_id)

    async def acknowledge(group_id: UUID) -> dict[str, Any]:
        return await (await router.for_group(group_id)).acknowledge_recovery(group_id)

    # Order matters: the fixed paths must precede /{group_id}, or "capabilities"
    # would be offered to the UUID parameter and fail validation.
    routes: list[tuple[str, str, Callable[..., Any]]] = [
        ("GET", "/v1/oco/capabilities", capabilities),
        ("GET", "/v1/oco/inventory", inventory),
        ("POST", "/v1/oco", submit),
        ("GET", "/v1/oco/{group_id}", get),
        ("POST", "/v1/oco/{group_id}/cancel", cancel),
        ("POST", "/v1/oco/{group_id}/close", close),
        ("POST", "/v1/oco/{group_id}/acknowledge", acknowledge),
    ]
    if mt5_aliases:
        routes += [
            ("GET", "/v1/mt5/capabilities", capabilities),
            ("GET", "/v1/mt5/inventory", inventory),
            ("POST", "/v1/mt5/oco", submit),
            ("GET", "/v1/mt5/oco/{group_id}", get),
            ("POST", "/v1/mt5/oco/{group_id}/cancel", cancel),
            ("POST", "/v1/mt5/oco/{group_id}/close", close),
            ("POST", "/v1/mt5/oco/{group_id}/acknowledge", acknowledge),
        ]
    for method, path, endpoint in routes:
        app.add_api_route(
            path,
            endpoint,
            methods=[method],
            dependencies=auth,
            tags=["oco"],
            include_in_schema=not path.startswith("/v1/mt5/"),
        )
