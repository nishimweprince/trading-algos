#!/usr/bin/env python3
"""Run the no-order meta-v2 Capital Demo shadow worker once or continuously."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.config import settings
from app.providers.capital import CapitalMarketDataClient
from app.services.capital_sync import CapitalCandleSync
from app.services.feature_refresh import refresh_h1_features
from app.services.meta_shadow_store import MetaShadowStore
from app.services.meta_shadow_worker import MetaShadowWorker
from app.services.pipeline_lock import PipelineLockedError, pipeline_lock


def _worker() -> MetaShadowWorker:
    required = {
        "LOOKUP_CAPITAL_API_KEY": settings.capital_api_key,
        "LOOKUP_CAPITAL_IDENTIFIER": settings.capital_identifier,
        "LOOKUP_CAPITAL_API_PASSWORD": settings.capital_api_password,
        "LOOKUP_CAPITAL_EPIC": settings.capital_epic,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise SystemExit(f"Missing private Capital settings: {', '.join(missing)}")
    client = CapitalMarketDataClient(
        api_key=settings.capital_api_key.get_secret_value(),
        identifier=settings.capital_identifier.get_secret_value(),
        api_password=settings.capital_api_password.get_secret_value(),
        environment=settings.capital_environment,
        price_side=settings.capital_price_side,
        settlement_seconds=settings.capital_settlement_seconds,
    )
    sync = CapitalCandleSync(
        client,
        data_dir=settings.data_dir,
        overlap_bars=settings.capital_overlap_bars,
        after_publish=refresh_h1_features,
    )
    return MetaShadowWorker(
        sync=sync,
        store=MetaShadowStore(settings.meta_shadow_db_path),
        epic=settings.capital_epic,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        with pipeline_lock(settings.data_dir / ".meta-shadow-worker.lock"):
            worker = _worker()
            while True:
                started = time.monotonic()
                try:
                    print(json.dumps(worker.run_once(), sort_keys=True), flush=True)
                except Exception as exc:
                    print(
                        json.dumps(
                            {
                                "status": "error",
                                "error": type(exc).__name__,
                                "detail": str(exc),
                            }
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                    if args.once:
                        raise
                if args.once:
                    return 0
                time.sleep(
                    max(
                        1.0,
                        settings.capital_poll_seconds - (time.monotonic() - started),
                    )
                )
    except PipelineLockedError as exc:
        raise SystemExit("Another meta shadow worker is already running") from exc


if __name__ == "__main__":
    raise SystemExit(main())
