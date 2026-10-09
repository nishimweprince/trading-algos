"""Dated ``exchangeInfo`` snapshots (public, unsigned).

Binance Vision has no history of symbol filters, so backtests use the newest
snapshot taken on or before the run, and record which one in their metadata.
Leverage brackets need a signed request and are fetched by the live runner
(M5); the backtester uses conservative defaults until then.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from zone_trend.data.store import DataPaths

__all__ = ["SymbolFilters", "fetch_exchange_info", "latest_filters"]

EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"


def fetch_exchange_info(paths: DataPaths, client: httpx.Client | None = None) -> Path:
    own = client is None
    client = client or httpx.Client(timeout=30)
    try:
        response = client.get(EXCHANGE_INFO_URL)
        response.raise_for_status()
        body = response.json()
    finally:
        if own:
            client.close()
    paths.exchange_info.mkdir(parents=True, exist_ok=True)
    target = paths.exchange_info / f"{datetime.now(UTC):%Y-%m-%d}.json"
    target.write_text(json.dumps(body))
    return target


class SymbolFilters(dict):
    """symbol -> {"tick", "step", "min_qty", "min_notional", "status", "contract_type",
    "underlying_type"}."""

    snapshot: str | None = None


def latest_filters(paths: DataPaths) -> SymbolFilters:
    """Filters from the newest snapshot; empty when none has been fetched."""
    out = SymbolFilters()
    files = sorted(paths.exchange_info.glob("*.json")) if paths.exchange_info.exists() else []
    if not files:
        return out
    out.snapshot = files[-1].stem
    for row in json.loads(files[-1].read_text()).get("symbols", []):
        by_type = {f["filterType"]: f for f in row.get("filters", [])}
        out[row["symbol"]] = {
            "tick": float(by_type.get("PRICE_FILTER", {}).get("tickSize", 0) or 0),
            "step": float(by_type.get("LOT_SIZE", {}).get("stepSize", 0) or 0),
            "min_qty": float(by_type.get("LOT_SIZE", {}).get("minQty", 0) or 0),
            "min_notional": float(by_type.get("MIN_NOTIONAL", {}).get("notional", 0) or 0),
            "status": row.get("status"),
            "contract_type": row.get("contractType"),
            "underlying_type": row.get("underlyingType"),
        }
    return out
