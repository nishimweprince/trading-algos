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

from app.config import settings
from app.providers.capital import CapitalMarketDataClient
from app.providers.instruments import capital_epic_for
from app.services.capital_sync import (
    CapitalCandleConflict,
    CapitalCandleSync,
    review_capital_conflicts,
)
from app.services.feature_refresh import refresh_h1_features


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
    parser = argparse.ArgumentParser(
        description="Sync Capital.com closed H1 bid candles"
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check-session", action="store_true")
    action.add_argument("--search-market")
    action.add_argument("--check-market", action="store_true")
    action.add_argument(
        "--review-conflicts",
        action="store_true",
        help="Summarize pending provider corrections without changing data",
    )
    action.add_argument(
        "--accept-conflict",
        type=Path,
        help="Apply one reviewed quarantine file and rebuild affected derivatives",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--symbol", default="XAUUSD")
    args = parser.parse_args()

    if args.review_conflicts:
        print(json.dumps(review_capital_conflicts(settings.data_dir), indent=2, sort_keys=True))
        return

    symbol = args.symbol.upper()
    epic = capital_epic_for(symbol, settings.capital_epics)
    client = _client()

    if args.accept_conflict:
        result = CapitalCandleSync(
            client,
            data_dir=settings.data_dir,
            overlap_bars=settings.capital_overlap_bars,
            after_publish=refresh_h1_features,
        ).accept_conflict(args.accept_conflict, symbol=symbol, epic=epic)
        print(json.dumps(asdict(result), indent=2, sort_keys=True, default=str))
        return

    if args.check_session:
        print(json.dumps(client.check_session(), sort_keys=True))
        return
    if args.search_market:
        print(
            json.dumps(
                client.search_markets(args.search_market), indent=2, sort_keys=True
            )
        )
        return
    if args.check_market:
        print(json.dumps(client.validate_market(epic), indent=2, sort_keys=True))
        return

    try:
        result = CapitalCandleSync(
            client,
            data_dir=settings.data_dir,
            overlap_bars=settings.capital_overlap_bars,
            after_publish=refresh_h1_features,
        ).sync(symbol=symbol, epic=epic, dry_run=args.dry_run)
    except CapitalCandleConflict as exc:
        print(str(exc), file=sys.stderr)
        print(
            "Next: review with '.venv/bin/python scripts/sync_capital.py "
            "--review-conflicts'.",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    payload = asdict(result)
    for key in ("latest_complete_candle", "histdata_cutoff", "capital_server_time"):
        value = payload[key]
        payload[key] = value.isoformat() if value else None
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
