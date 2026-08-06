#!/usr/bin/env python3
"""Synchronize read-only OANDA v20 midpoint candles into local Parquet."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.config import settings  # noqa: E402
from app.db.duck import refresh_parquet_views  # noqa: E402
from app.providers.oanda import OandaV20Provider  # noqa: E402
from app.services.oanda_sync import OandaCandleSync, storage_to_oanda  # noqa: E402


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamps must include a UTC offset or Z")
    return parsed.astimezone(UTC)


def _provider() -> OandaV20Provider:
    if settings.oanda_token is None or settings.oanda_account_id is None:
        raise SystemExit(
            "Missing credentials: set LOOKUP_OANDA_TOKEN and LOOKUP_OANDA_ACCOUNT_ID"
        )
    return OandaV20Provider(
        token=settings.oanda_token.get_secret_value(),
        account_id=settings.oanda_account_id.get_secret_value(),
        environment=settings.oanda_environment,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync complete OANDA H1/H4 candles")
    parser.add_argument("--symbol", default="XAUUSD", choices=["XAUUSD"])
    parser.add_argument("--timeframe", default="H1", choices=["H1", "H4"])
    parser.add_argument("--start", type=_timestamp)
    parser.add_argument("--end", type=_timestamp)
    parser.add_argument("--dry-run", action="store_true", help="Fetch and validate without writing")
    parser.add_argument(
        "--check-instrument",
        action="store_true",
        help="Validate account instrument availability without fetching candles",
    )
    args = parser.parse_args()

    provider = _provider()
    instrument = storage_to_oanda(args.symbol)
    if args.check_instrument:
        provider.validate_instrument(instrument)
        print(
            json.dumps(
                {
                    "status": "available",
                    "symbol": args.symbol,
                    "instrument": instrument,
                    "environment": settings.oanda_environment,
                },
                sort_keys=True,
            )
        )
        return

    end = args.end or datetime.now(UTC)
    start = args.start or end - timedelta(days=settings.oanda_initial_backfill_days)
    synchronizer = OandaCandleSync(
        provider,
        settings.data_dir / "candles",
        refresh_views=lambda: refresh_parquet_views(candles=True),
        overlap_bars=settings.oanda_overlap_bars,
    )
    result = synchronizer.sync(
        symbol=args.symbol,
        timeframe=args.timeframe,
        start=start,
        end=end,
        dry_run=args.dry_run,
    )
    payload = asdict(result)
    payload["latest_complete_candle"] = (
        result.latest_complete_candle.isoformat() if result.latest_complete_candle else None
    )
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
