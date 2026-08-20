from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
from pydantic import BaseModel, ValidationError

from api import create_app
from candles import CandleStore
from comparison import compare_entry_modes
from config import Settings, load_settings, resolve_env_file
from logging_config import configure_logging, log_event
from models import TIMEFRAME_MINUTES, Candle, EngineParams, ScaleSweepReport, Timeframe
from research.render import render_scale_sweep_markdown
from research.s1_target_hit import render_s1_markdown, run_s1_target_hit
from research.s2_break_frequency import render_s2_markdown, run_s2_break_frequency
from research.s3_anchor_study import render_s3_markdown, run_s3_anchor_study
from research.s4_cost_sensitivity import render_s4_markdown, run_s4_cost_sensitivity
from research.s9_regime import render_s9_markdown, run_s9_regime_attribution
from research.scale import run_scale_sweep


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

    if args.run_s8_scale_sweep:
        sys.exit(_run_s8_scale_sweep(settings, args))

    if args.run_s1_target_hit:
        sys.exit(_run_s1_target_hit(settings, args))

    if args.run_s2_break_frequency:
        sys.exit(_run_s2_break_frequency(settings, args))

    if args.run_s3_anchor_study:
        sys.exit(_run_s3_anchor_study(settings, args))

    if args.run_s4_cost_sensitivity:
        sys.exit(_run_s4_cost_sensitivity(settings, args))

    if args.run_s9_regime_attribution:
        sys.exit(_run_s9_regime_attribution(settings, args))

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


def _run_s8_scale_sweep(settings: Settings, args: argparse.Namespace) -> int:
    """S8 is defined on M15; refuse any other timeframe rather than silently rescaling."""
    symbol = (args.symbol or settings.symbol).upper()
    if args.timeframe and Timeframe(args.timeframe) is not Timeframe.M15:
        print(
            f"--run-s8-scale-sweep is defined on M15 only, not {args.timeframe}",
            file=sys.stderr,
        )
        return 1
    timeframe = Timeframe.M15

    async def _run() -> int:
        async with httpx.AsyncClient() as http:
            store = CandleStore(settings, http)
            candles = store.load_local(
                symbol,
                timeframe,
                date_from=args.date_from,
                date_to=args.date_to,
            )
            # Unfiltered: an M1 bar just before ``date_from`` still covers the first
            # parent bar, and coverage is measured against the parent bars themselves.
            m1_bars = store.load_local(symbol, Timeframe.M1)
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
        report = run_scale_sweep(
            candles,
            settings.session_windows(),
            params,
            settings.session_anchors(),
            symbol=symbol,
            timeframe=timeframe,
            source="local",
            m1_bars=m1_bars,
        )
        json_path, markdown_path = write_scale_sweep(report, args.output_dir)
        print(
            f"Wrote {len(report.cells)} S8 cells to {json_path} and {markdown_path} "
            f"(fingerprint {report.candle_set_sha256}, M1 coverage {report.m1_coverage.status})"
        )
        return 0

    return asyncio.run(_run())


def write_scale_sweep(report: ScaleSweepReport, output_dir: Path) -> tuple[Path, Path]:
    """Write the machine-readable surface and its rendered Markdown side by side."""
    return write_research_report(
        report,
        render_scale_sweep_markdown(report),
        output_dir,
        "s8-scale-decomposition",
    )


@dataclass(frozen=True)
class ResearchInputs:
    """One immutable M15 candle set plus the configuration every study shares."""

    symbol: str
    timeframe: Timeframe
    candles: list[Candle]
    m1_bars: list[Candle]
    params: EngineParams


def _load_research_inputs(
    settings: Settings, args: argparse.Namespace, *, study: str
) -> ResearchInputs | int:
    """Load the shared M15 inputs, or return the exit code to fail with."""
    symbol = (args.symbol or settings.symbol).upper()
    if args.timeframe and Timeframe(args.timeframe) is not Timeframe.M15:
        print(f"{study} is defined on M15 only, not {args.timeframe}", file=sys.stderr)
        return 1
    timeframe = Timeframe.M15

    async def _load() -> tuple[list[Candle], list[Candle]]:
        async with httpx.AsyncClient() as http:
            store = CandleStore(settings, http)
            candles = store.load_local(
                symbol, timeframe, date_from=args.date_from, date_to=args.date_to
            )
            # Unfiltered: an M1 bar just before ``date_from`` still covers the first
            # parent bar, and coverage is measured against the parent bars themselves.
            return candles, store.load_local(symbol, Timeframe.M1)

    candles, m1_bars = asyncio.run(_load())
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
    return ResearchInputs(
        symbol=symbol,
        timeframe=timeframe,
        candles=candles,
        m1_bars=m1_bars,
        params=params,
    )


def write_research_report(
    report: BaseModel, markdown: str, output_dir: Path, stem: str
) -> tuple[Path, Path]:
    """Write one study's machine-readable surface and its rendered Markdown."""
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{stem}.json"
    markdown_path = output_dir / f"{stem}.md"
    json_path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return json_path, markdown_path


def _run_s1_target_hit(settings: Settings, args: argparse.Namespace) -> int:
    loaded = _load_research_inputs(settings, args, study="--run-s1-target-hit")
    if isinstance(loaded, int):
        return loaded
    report = run_s1_target_hit(
        loaded.candles,
        settings.session_windows(),
        loaded.params,
        settings.session_anchors(),
        symbol=loaded.symbol,
        timeframe=loaded.timeframe,
        source="local",
        m1_bars=loaded.m1_bars,
    )
    json_path, markdown_path = write_research_report(
        report, render_s1_markdown(report), args.output_dir, "s1-conditional-target-hit"
    )
    print(
        f"Wrote S1 to {json_path} and {markdown_path}: "
        f"{report.conditioning.conditioned} conditioned structures of "
        f"{report.conditioning.structures_total}, M1 coverage {report.m1_coverage.status}"
    )
    return 0


def _run_s2_break_frequency(settings: Settings, args: argparse.Namespace) -> int:
    loaded = _load_research_inputs(settings, args, study="--run-s2-break-frequency")
    if isinstance(loaded, int):
        return loaded
    report = run_s2_break_frequency(
        loaded.candles,
        settings.session_windows(),
        loaded.params,
        settings.session_anchors(),
        symbol=loaded.symbol,
        timeframe=loaded.timeframe,
        source="local",
        m1_bars=loaded.m1_bars,
    )
    json_path, markdown_path = write_research_report(
        report, render_s2_markdown(report), args.output_dir, "s2-break-frequency"
    )
    print(
        f"Wrote S2 to {json_path} and {markdown_path}: {report.episodes_total} episodes "
        f"across {len(report.horizon_hours)} horizons, M1 coverage {report.m1_coverage.status}"
    )
    return 0


def _run_s3_anchor_study(settings: Settings, args: argparse.Namespace) -> int:
    loaded = _load_research_inputs(settings, args, study="--run-s3-anchor-study")
    if isinstance(loaded, int):
        return loaded
    report = run_s3_anchor_study(
        loaded.candles,
        settings.session_windows(),
        loaded.params,
        symbol=loaded.symbol,
        timeframe=loaded.timeframe,
        source="local",
        m1_bars=loaded.m1_bars,
    )
    json_path, markdown_path = write_research_report(
        report, render_s3_markdown(report), args.output_dir, "s3-anchor-study"
    )
    print(
        f"Wrote S3 to {json_path} and {markdown_path}: {len(report.cells)} anchor variants, "
        f"M1 coverage {report.m1_coverage.status}"
    )
    return 0


def _run_s4_cost_sensitivity(settings: Settings, args: argparse.Namespace) -> int:
    loaded = _load_research_inputs(settings, args, study="--run-s4-cost-sensitivity")
    if isinstance(loaded, int):
        return loaded
    report = run_s4_cost_sensitivity(
        loaded.candles,
        settings.session_windows(),
        loaded.params,
        settings.session_anchors(),
        symbol=loaded.symbol,
        timeframe=loaded.timeframe,
        source="local",
        m1_bars=loaded.m1_bars,
    )
    json_path, markdown_path = write_research_report(
        report, render_s4_markdown(report), args.output_dir, "s4-cost-sensitivity"
    )
    print(
        f"Wrote S4 to {json_path} and {markdown_path}: {len(report.cells)} cost cells, "
        f"M1 coverage {report.m1_coverage.status}"
    )
    return 0


def _run_s9_regime_attribution(settings: Settings, args: argparse.Namespace) -> int:
    loaded = _load_research_inputs(settings, args, study="--run-s9-regime-attribution")
    if isinstance(loaded, int):
        return loaded
    report = run_s9_regime_attribution(
        loaded.candles,
        settings.session_windows(),
        loaded.params,
        settings.session_anchors(),
        symbol=loaded.symbol,
        timeframe=loaded.timeframe,
        source="local",
        m1_bars=loaded.m1_bars,
    )
    json_path, markdown_path = write_research_report(
        report, render_s9_markdown(report), args.output_dir, "s9-regime-attribution"
    )
    print(
        f"Wrote S9 to {json_path} and {markdown_path}: {len(report.cells)} split cells, "
        f"{len(report.flags)} directional flags, M1 coverage {report.m1_coverage.status}"
    )
    return 0


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
