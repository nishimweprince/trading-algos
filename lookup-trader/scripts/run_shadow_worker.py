#!/usr/bin/env python3
"""Run the no-order Capital.com forward-shadow worker once or continuously."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.config import settings  # noqa: E402
from app.providers.capital import CapitalMarketDataClient  # noqa: E402
from app.services.capital_sync import CapitalCandleSync  # noqa: E402
from app.services.feature_refresh import refresh_h1_features  # noqa: E402
from app.services.pipeline_lock import PipelineLockedError, pipeline_lock  # noqa: E402
from app.services.shadow_store import ShadowStore  # noqa: E402
from app.services.shadow_worker import ShadowWorker  # noqa: E402


def _worker() -> ShadowWorker:
    required = {
        "LOOKUP_CAPITAL_API_KEY": settings.capital_api_key,
        "LOOKUP_CAPITAL_IDENTIFIER": settings.capital_identifier,
        "LOOKUP_CAPITAL_API_PASSWORD": settings.capital_api_password,
        "LOOKUP_CAPITAL_EPIC": settings.capital_epic,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise SystemExit(f"Missing private Capital.com settings: {', '.join(missing)}")
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
    return ShadowWorker(
        sync=sync,
        store=ShadowStore(settings.shadow_db_path),
        artifact_version=settings.outcome_artifact_version,
        epic=settings.capital_epic,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Capital.com no-order shadow worker")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    args = parser.parse_args()
    try:
        with pipeline_lock(settings.data_dir / ".shadow-worker.lock"):
            worker = _worker()
            while True:
                started = time.monotonic()
                try:
                    print(json.dumps(worker.run_once(), sort_keys=True), flush=True)
                except Exception as exc:
                    print(
                        json.dumps(
                            {"status": "error", "error": type(exc).__name__, "detail": str(exc)},
                            sort_keys=True,
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                    if args.once:
                        raise
                if args.once:
                    return
                time.sleep(max(1.0, settings.capital_poll_seconds - (time.monotonic() - started)))
    except PipelineLockedError as exc:
        raise SystemExit("Another shadow worker is already running") from exc


if __name__ == "__main__":
    main()
