from __future__ import annotations

import argparse
import asyncio
import sys

import httpx
import uvicorn
from pydantic import ValidationError

from api import create_app
from candles import CandleStore
from config import Settings, load_settings, resolve_env_file
from logging_config import configure_logging, log_event
from models import Timeframe


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Session-open hedge backtest and paper service")
    parser.add_argument("--profile", metavar="NAME", help="Load .env.NAME instead of .env")
    one_shot = parser.add_mutually_exclusive_group()
    one_shot.add_argument(
        "--validate-config",
        action="store_true",
        help="Validate the environment and session windows, then exit",
    )
    one_shot.add_argument(
        "--seed",
        action="store_true",
        help="Fetch closed candles from ctrader-markets into data/, then exit",
    )
    one_shot.add_argument(
        "--seed-m1",
        action="store_true",
        help="Fetch closed M1 candles into data/candles/<SYMBOL>/M1.jsonl, then exit",
    )
    parser.add_argument("--symbol", help="Override SYMBOL for --seed / --seed-m1")
    parser.add_argument(
        "--timeframe",
        choices=[tf.value for tf in Timeframe],
        help="Timeframe to seed with --seed (default M15; ignored by --seed-m1)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        help="Bars to seed (default 2000, or 20000 for --seed-m1)",
    )
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        settings = load_settings(args.profile)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    except ValidationError as exc:
        print(f"Invalid configuration in {resolve_env_file(args.profile)}:", file=sys.stderr)
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "(root)"
            print(f"  {location}: {error['msg']}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.validate_config:
        windows = settings.session_windows()
        print(
            f"Valid configuration: {settings.symbol} {settings.timeframe.value} "
            f"sessions={[w.name for w in windows]}"
        )
        return

    if args.seed or args.seed_m1:
        sys.exit(_seed(settings, args))

    configure_logging(settings.log_level)
    log_event(
        "http_server_starting",
        host=settings.host,
        port=settings.port,
        symbol=settings.symbol,
        timeframe=settings.timeframe.value,
        paper_enabled=settings.paper_enabled,
    )

    def app_factory() -> object:
        return create_app(settings)

    uvicorn.run(
        app_factory,
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=1,
        access_log=False,
    )


def _seed_timeframe(args: argparse.Namespace, settings: Settings) -> Timeframe:
    if args.seed_m1:
        return Timeframe.M1
    if args.timeframe:
        return Timeframe(args.timeframe)
    return settings.timeframe


def _seed_count(args: argparse.Namespace, timeframe: Timeframe) -> int:
    if args.count is not None:
        return args.count
    return 20_000 if timeframe is Timeframe.M1 else 2000


def _seed(settings: Settings, args: argparse.Namespace) -> int:
    configure_logging(settings.log_level)
    symbol = (args.symbol or settings.symbol).upper()
    timeframe = _seed_timeframe(args, settings)
    count = _seed_count(args, timeframe)

    async def _run() -> int:
        async with httpx.AsyncClient() as http:
            store = CandleStore(settings, http)
            try:
                candles = await store.fetch_ctrader(symbol, timeframe, count=count)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    print(
                        "ctrader-markets returned 401 Unauthorized. Set CTRADER_API_KEY "
                        "in .env to the gateway API_KEY "
                        f"({settings.ctrader_markets_url}).",
                        file=sys.stderr,
                    )
                    return 1
                print(f"ctrader-markets request failed: {exc}", file=sys.stderr)
                return 1
            if not candles:
                print("No candles returned from ctrader-markets", file=sys.stderr)
                return 1
            path = store.write_local(symbol, timeframe, candles)
            print(f"Wrote {len(candles)} {symbol} {timeframe.value} bars to {path}")
            return 0

    return asyncio.run(_run())


if __name__ == "__main__":
    run()
