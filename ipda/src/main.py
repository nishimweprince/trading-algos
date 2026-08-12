"""Entrypoint: build the components and run the poll loop.

Run exactly one instance per profile to avoid duplicate trading decisions.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import httpx

from .config import Settings, load_settings
from .data_client import MarketDataClient
from .instruments import instrument_summary
from .logging_config import RuntimeLogs, configure_logging, log_event
from .mt5_client import Mt5TraderClient
from .service import SignalService


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="IPDA Supertrend signal service")
    parser.add_argument(
        "--profile",
        metavar="NAME",
        help="Load .env.NAME instead of .env",
    )
    return parser.parse_args(argv)


async def amain(settings: Settings) -> None:
    configure_logging(settings.log_level)
    logs = RuntimeLogs(settings.logs_dir)

    log_event(
        "startup",
        profile=settings.profile,
        instruments=[
            instrument_summary(instrument, settings) for instrument in settings.instruments
        ],
        target_tf_minutes=settings.target_tf_minutes,
        poll_interval_seconds=settings.poll_interval_seconds,
    )

    async with httpx.AsyncClient() as http:
        service = SignalService(
            settings=settings,
            data_client=MarketDataClient(settings, http),
            mt5_client=Mt5TraderClient(settings, http),
            logs=logs,
        )
        while True:
            try:
                await service.tick()
            except Exception:  # noqa: BLE001 - never let one tick kill the loop
                log_event("tick_failed", level=logging.ERROR, exc_info=True)
            await asyncio.sleep(settings.poll_interval_seconds)


def run(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        settings = load_settings(args.profile)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)

    try:
        asyncio.run(amain(settings))
    except KeyboardInterrupt:
        log_event("shutdown")


if __name__ == "__main__":
    run()
