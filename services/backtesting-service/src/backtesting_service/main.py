from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
from pydantic import ValidationError
from ta_clients import CandleStore
from ta_contracts import TIMEFRAME_MINUTES

from .api import create_app
from .comparison import compare_entry_modes
from .config import Settings, load_settings, resolve_env_file
from .logging_config import configure_logging, log_event
from .models import EngineParams, Timeframe
from .research import cli as research_cli


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
    one_shot.add_argument(
        "--compare-entry-modes",
        action="store_true",
        help="Run one local candle set through all four Phase 2 entry modes, then exit",
    )
    one_shot.add_argument(
        "--run-phase3-gate-scorecard",
        action="store_true",
        help="Evaluate every §9 gate from committed research artifacts, then exit",
    )
    one_shot.add_argument(
        "--run-phase3-post-s6-s7-scorecard",
        action="store_true",
        help=(
            "Write a post-S6/S7 scorecard without overwriting the original blocking "
            "scorecard, then exit"
        ),
    )
    one_shot.add_argument(
        "--run-phase3-exploratory",
        action="store_true",
        help=(
            "Run the frozen §8.0 development protocol on the 9,998-bar cache, "
            "write phase3-exploratory-development.{json,md}, then exit. "
            "Does not unlock or evaluate the prospective holdout."
        ),
    )
    one_shot.add_argument(
        "--run-phase3-holdout",
        action="store_true",
        help=(
            "Inspect P3H-20260820 metadata and refuse strategy evaluation unless the "
            "complete §8.0 unlock manifest and 4,000 prospective bars both exist"
        ),
    )
    one_shot.add_argument(
        "--run-hedge-survivor-development",
        action="store_true",
        help=(
            "Run the frozen H1 hedge-survivor candidate family with portfolio and "
            "matched-opportunity replays; the external holdout remains locked"
        ),
    )
    one_shot.add_argument(
        "--run-s8-scale-sweep",
        action="store_true",
        help=(
            "Run the S8 256-cell scale decomposition over one local M15 candle set, "
            "write reports/research/s8-scale-decomposition.{json,md}, then exit"
        ),
    )
    one_shot.add_argument(
        "--run-s1-target-hit",
        action="store_true",
        help="Run the S1 conditional target-hit study over one local M15 candle set, then exit",
    )
    one_shot.add_argument(
        "--run-s2-break-frequency",
        action="store_true",
        help="Run the S2 single-break versus double-break study, then exit",
    )
    one_shot.add_argument(
        "--run-s3-anchor-study",
        action="store_true",
        help="Run the S3 anchor grid study, then exit",
    )
    one_shot.add_argument(
        "--run-s4-cost-sensitivity",
        action="store_true",
        help="Run the S4 cost sensitivity and break-even sweep, then exit",
    )
    one_shot.add_argument(
        "--run-s5-resolver-bias",
        action="store_true",
        help="Run identical configuration through resolver tiers 0-4, then exit",
    )
    one_shot.add_argument(
        "--run-s6-walk-forward",
        action="store_true",
        help="Run the frozen S6 nested walk-forward protocol, then exit",
    )
    one_shot.add_argument(
        "--run-s7-prop-monte-carlo",
        action="store_true",
        help="Run the seeded S7 complete-cluster PropGuard Monte Carlo, then exit",
    )
    one_shot.add_argument(
        "--run-s9-regime-attribution",
        action="store_true",
        help="Run the S9 regime and trend attribution study, then exit",
    )
    parser.add_argument("--symbol", help="Override SYMBOL for seeding or comparison")
    parser.add_argument(
        "--timeframe",
        choices=[tf.value for tf in Timeframe],
        help="Timeframe to seed or compare (uses configured timeframe by default)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        help="Bars to seed (default 2000, or 20000 for --seed-m1)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports/research"),
        help="Directory for research artifacts (default reports/research)",
    )
    parser.add_argument(
        "--date-from",
        type=_aware_datetime,
        help="Inclusive ISO-8601 comparison start with timezone",
    )
    parser.add_argument(
        "--date-to",
        type=_aware_datetime,
        help="Inclusive ISO-8601 comparison end with timezone",
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

    if args.compare_entry_modes:
        sys.exit(_compare_entry_modes(settings, args))

    for flag, driver in research_cli.COMMANDS:
        if getattr(args, flag):
            sys.exit(driver(settings, args))

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


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone offset")
    return parsed


def _compare_entry_modes(settings: Settings, args: argparse.Namespace) -> int:
    symbol = (args.symbol or settings.symbol).upper()
    timeframe = Timeframe(args.timeframe) if args.timeframe else settings.timeframe

    async def _run() -> int:
        async with httpx.AsyncClient() as http:
            store = CandleStore(settings, http)
            candles = store.load_local(
                symbol,
                timeframe,
                date_from=args.date_from,
                date_to=args.date_to,
            )
        if not candles:
            print(
                f"No local candles for {symbol} {timeframe.value} in the requested range",
                file=sys.stderr,
            )
            return 1
        params = EngineParams.model_validate(
            settings.engine_params().model_dump()
            | {"timeframe_minutes": TIMEFRAME_MINUTES[timeframe]}
        )
        report = compare_entry_modes(
            candles,
            settings.session_windows(),
            params,
            settings.session_anchors(),
            symbol=symbol,
            timeframe=timeframe,
            source="local",
        )
        print(report.model_dump_json(indent=2))
        return 0

    return asyncio.run(_run())


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
