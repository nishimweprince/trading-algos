"""Entrypoint: build the components and run the poll loop.

Run exactly one instance per profile to avoid duplicate trading decisions.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import httpx
from pydantic import ValidationError

from .config import Settings, load_settings
from .data_client import MarketDataClient
from .instruments import instrument_summary
from .logging_config import RuntimeLogs, configure_logging, log_event
from .mt5_client import Mt5TraderClient
from .notifier import Notifier
from .position_tracker import PositionTracker
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

    tracker: PositionTracker | None = None
    if settings.track_open_trades:
        tracker = PositionTracker(
            state_path=settings.logs_dir / "open_trades.json",
            break_even_pips=settings.mfe_break_even_pips,
            ttl_hours=settings.tracked_trade_ttl_hours,
        )
        tracker.load()

    log_event(
        "startup",
        profile=settings.profile,
        instruments=[
            instrument_summary(instrument, settings) for instrument in settings.instruments
        ],
        target_tf_minutes=settings.target_tf_minutes,
        poll_interval_seconds=settings.poll_interval_seconds,
        trigger="reversal",
        reversal_sensitivity=settings.reversal_rsi_len,
        reversal_levels=[settings.reversal_oversold, settings.reversal_overbought],
        trading_sessions=settings.trading_sessions or ["always"],
        notifications_enabled=settings.notifications_enabled,
        notification_channels=sorted(settings.notification_channels),
        mfe_break_even_pips=settings.mfe_break_even_pips if tracker else None,
        tracked_trades_restored=len(tracker.trades) if tracker else 0,
    )

    async with httpx.AsyncClient() as http:
        service = SignalService(
            settings=settings,
            data_client=MarketDataClient(settings, http),
            mt5_client=Mt5TraderClient(settings, http),
            logs=logs,
            notifier=Notifier(settings, http),
            tracker=tracker,
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
    except (FileNotFoundError, ValidationError, ValueError) as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)

    try:
        asyncio.run(amain(settings))
    except KeyboardInterrupt:
        log_event("shutdown")


if __name__ == "__main__":
    run()
