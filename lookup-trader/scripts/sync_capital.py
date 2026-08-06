#!/usr/bin/env python3
"""Validate or synchronize Capital.com Demo closed H1 bid candles."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.config import settings  # noqa: E402
from app.providers.capital import CapitalMarketDataClient  # noqa: E402
from app.services.capital_sync import CapitalCandleSync  # noqa: E402
from app.services.feature_refresh import refresh_h1_features  # noqa: E402


def _client() -> CapitalMarketDataClient:
    required = {
        "LOOKUP_CAPITAL_API_KEY": settings.capital_api_key,
        "LOOKUP_CAPITAL_IDENTIFIER": settings.capital_identifier,
        "LOOKUP_CAPITAL_API_PASSWORD": settings.capital_api_password,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise SystemExit(f"Missing private Capital.com settings: {', '.join(missing)}")
    return CapitalMarketDataClient(
        api_key=settings.capital_api_key.get_secret_value(),
        identifier=settings.capital_identifier.get_secret_value(),
        api_password=settings.capital_api_password.get_secret_value(),
        environment=settings.capital_environment,
        price_side=settings.capital_price_side,
        settlement_seconds=settings.capital_settlement_seconds,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Capital.com closed H1 bid candles")
    parser.add_argument("--check-session", action="store_true")
    parser.add_argument("--search-market")
    parser.add_argument("--check-market", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    client = _client()

    if args.check_session:
        print(json.dumps(client.check_session(), sort_keys=True))
        return
    if args.search_market:
        print(json.dumps(client.search_markets(args.search_market), indent=2, sort_keys=True))
        return
    epic = settings.capital_epic
    if not epic:
        raise SystemExit("Set LOOKUP_CAPITAL_EPIC after running --search-market Gold")
    if args.check_market:
        print(json.dumps(client.validate_market(epic), indent=2, sort_keys=True))
        return

    result = CapitalCandleSync(
        client,
        data_dir=settings.data_dir,
        overlap_bars=settings.capital_overlap_bars,
        after_publish=refresh_h1_features,
    ).sync(symbol="XAUUSD", epic=epic, dry_run=args.dry_run)
    payload = asdict(result)
    for key in ("latest_complete_candle", "histdata_cutoff", "capital_server_time"):
        value = payload[key]
        payload[key] = value.isoformat() if value else None
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
