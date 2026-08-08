from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn

from api import create_app
from config import Settings, load_settings
from logging_config import configure_logging


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="cTrader Open API market-data service")
    parser.add_argument(
        "--profile",
        metavar="NAME",
        help="Load .env.NAME instead of .env",
    )
    one_shot = parser.add_mutually_exclusive_group()
    one_shot.add_argument(
        "--discover-accounts",
        action="store_true",
        help="Print every ctidTraderAccountId reachable with the access token, then exit",
    )
    one_shot.add_argument(
        "--discover-symbols",
        action="store_true",
        help="Print the broker's symbolId/symbolName/digits table, then exit",
    )
    one_shot.add_argument(
        "--refresh-token",
        action="store_true",
        help="Rotate the OAuth token pair, persist it, then exit",
    )
    return parser.parse_args(argv)


def _run_one_shot(args: argparse.Namespace, settings: Settings) -> int | None:
    from . import discover

    configure_logging(settings.log_level)
    if args.discover_accounts:
        return asyncio.run(discover.discover_accounts(settings))
    if args.discover_symbols:
        return asyncio.run(discover.discover_symbols(settings))
    if args.refresh_token:
        return asyncio.run(discover.refresh_token(settings))
    return None


def run(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        settings = load_settings(args.profile)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)

    exit_code = _run_one_shot(args, settings)
    if exit_code is not None:
        sys.exit(exit_code)

    def app_factory() -> object:
        return create_app(settings=settings)

    uvicorn.run(
        app_factory,
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    run()
