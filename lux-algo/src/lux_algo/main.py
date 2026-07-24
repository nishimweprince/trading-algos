"""Entrypoint: build the components and run the poll loop.

Run exactly one instance (like mt5-trader) to avoid duplicate trading decisions.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from .config import Settings
from .data_client import MarketDataClient
from .logging_config import RuntimeLogs, configure_logging, log_event
from .mt5_client import Mt5TraderClient
from .service import SignalService


async def amain() -> None:
    settings = Settings()  # type: ignore[call-arg]  # values come from env/.env
    configure_logging(settings.log_level)
    logs = RuntimeLogs(settings.logs_dir)

    log_event(
        "startup",
        quote=settings.quote,
        symbol=settings.mt5_symbol,
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


def run() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log_event("shutdown")


if __name__ == "__main__":
    run()
