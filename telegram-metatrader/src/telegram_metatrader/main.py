from __future__ import annotations

import asyncio
import logging

from .config import load_config
from .health import start_health_server
from .jsonl import RuntimeLogs
from .mt5_executor import Mt5Executor
from .service import CopierService
from .telegram_client import create_client


async def amain() -> None:
    config = load_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("telegram_metatrader")
    logs = RuntimeLogs(config.logs_dir)
    client = await create_client(config)
    executor = Mt5Executor(config)
    service = CopierService(config, client, executor, logs, logger)
    start_health_server(config.http_host, config.http_port, service.status)
    logger.info(
        "Starting Telegram to MT5 copier in %s mode on %s:%s",
        "live" if config.live_trading_enabled else "dry-run",
        config.http_host,
        config.http_port,
    )
    try:
        await service.run_forever()
    finally:
        executor.shutdown()
        await client.disconnect()


def run() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    run()

