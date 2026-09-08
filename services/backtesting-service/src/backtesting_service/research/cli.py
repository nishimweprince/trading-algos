"""Command-line drivers for the research studies.

``research/`` already held S1-S9, walk-forward, monte carlo and the renderers.
What was still in ``main.py`` was the layer that drives them: argument
unpacking, loading the shared candle set, running the study, writing the JSON
and its rendered Markdown. That is ~480 lines of research code living in the
service entry point, and it is what migration-spec.md section 3.4 meant by
"extract research/".

``main.py`` keeps argument parsing and dispatch; every function here takes the
parsed ``argparse.Namespace`` and returns a process exit code.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import BaseModel
from ta_clients import CandleStore
from ta_contracts import TIMEFRAME_MINUTES

from ..anchors import anchor_from_window
from ..config import Settings
from ..models import Candle, EngineParams, ScaleSweepReport, Timeframe
from ..sessions import DEFAULT_SESSION_SPECS, build_windows
from .gate_scorecard import (
    build_phase3_gate_scorecard,
    render_phase3_gate_scorecard_markdown,
)
from .hedge_survivor import (
    DEVELOPMENT_FIRST_TS,
    DEVELOPMENT_LAST_TS,
    run_survivor_development,
    write_survivor_development,
)
from .phase3_exploratory import (
    DEVELOPMENT_STEM,
    DevelopmentCacheError,
    EvalBudgetExceeded,
    run_phase3_exploratory,
    write_phase3_exploratory_reports,
)
from .phase3_holdout import (
    HOLDOUT_ID,
    MANIFEST_STEM,
    holdout_path,
    holdout_ready_errors,
    inspect_holdout_file,
    load_holdout_manifest,
)
from .post_s6_s7_scorecard import (
    POST_SCORECARD_STEM,
    build_post_s6_s7_scorecard,
    render_post_s6_s7_scorecard_markdown,
)
from .render import render_scale_sweep_markdown
from .s1_target_hit import render_s1_markdown, run_s1_target_hit
from .s2_break_frequency import render_s2_markdown, run_s2_break_frequency
from .s3_anchor_study import render_s3_markdown, run_s3_anchor_study
from .s4_cost_sensitivity import render_s4_markdown, run_s4_cost_sensitivity
from .s5_resolver_bias import render_s5_markdown, run_s5_resolver_bias
from .s6_walk_forward import render_s6_markdown, run_s6_walk_forward
from .s7_prop_monte_carlo import render_s7_markdown, run_s7_prop_monte_carlo
from .s9_regime import render_s9_markdown, run_s9_regime_attribution
from .scale import run_scale_sweep


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
        settings.engine_params().model_dump() | {"timeframe_minutes": TIMEFRAME_MINUTES[timeframe]}
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


def write_scale_sweep(report: ScaleSweepReport, output_dir: Path) -> tuple[Path, Path]:
    """Write the machine-readable surface and its rendered Markdown side by side."""
    return write_research_report(
        report,
        render_scale_sweep_markdown(report),
        output_dir,
        "s8-scale-decomposition",
    )


def _run_phase3_gate_scorecard(args: argparse.Namespace) -> int:
    report = build_phase3_gate_scorecard(args.output_dir)
    json_path = args.output_dir / "phase3-gate-scorecard.json"
    markdown_path = args.output_dir / "phase3-gate-scorecard.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_phase3_gate_scorecard_markdown(report), encoding="utf-8")
    print(
        f"Wrote {report['gate_count']} §9 gates to {json_path} and {markdown_path}: "
        f"{report['verdict_counts']['pass']} pass; "
        f"Phase 3 authorized={str(report['phase3_redesign_authorized']).lower()}"
    )
    return 0


def _run_phase3_post_s6_s7_scorecard(args: argparse.Namespace) -> int:
    report = build_post_s6_s7_scorecard(args.output_dir)
    json_path = args.output_dir / f"{POST_SCORECARD_STEM}.json"
    markdown_path = args.output_dir / f"{POST_SCORECARD_STEM}.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_post_s6_s7_scorecard_markdown(report), encoding="utf-8")
    print(
        f"Wrote post-S6/S7 scorecard to {json_path} and {markdown_path}: "
        f"edge={report['blocking_gate_verdicts']['edge_reality']}; "
        f"authorized={str(report['phase3_redesign_authorized']).lower()}; "
        "original phase3-gate-scorecard left in place"
    )
    return 0


def _run_phase3_exploratory(settings: Settings, args: argparse.Namespace) -> int:
    if args.date_from is not None or args.date_to is not None:
        print(
            "--run-phase3-exploratory must use the entire frozen 9,998-bar development cache",
            file=sys.stderr,
        )
        return 1
    loaded = _load_research_inputs(settings, args, study="--run-phase3-exploratory")
    if isinstance(loaded, int):
        return loaded
    if loaded.symbol != "XAUUSD":
        print("--run-phase3-exploratory is defined on XAUUSD only", file=sys.stderr)
        return 1
    cache_path = settings.local_candles_path(loaded.symbol, loaded.timeframe)
    windows = build_windows(["tokyo", "london", "new_york"], DEFAULT_SESSION_SPECS)
    anchors = [anchor_from_window(window) for window in windows]
    try:
        report = run_phase3_exploratory(
            loaded.candles,
            windows,
            loaded.params,
            anchors,
            symbol="XAUUSD",
            timeframe=Timeframe.M15,
            source="local",
            m1_bars=loaded.m1_bars,
            cache_path=cache_path,
        )
    except (DevelopmentCacheError, EvalBudgetExceeded) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    json_path, markdown_path = write_phase3_exploratory_reports(report, args.output_dir)
    selected = report["full_development"]["selected"]
    selected_id = selected["coordinate_id"] if selected else "none"
    print(
        f"Wrote {DEVELOPMENT_STEM} to {json_path} and {markdown_path}: "
        f"{report['evaluation_count']} evaluations, selected={selected_id}, "
        f"holdout={report['holdout_status']}"
    )
    return 0


def _run_phase3_holdout(settings: Settings, args: argparse.Namespace) -> int:
    manifest = load_holdout_manifest(args.output_dir / f"{MANIFEST_STEM}.json")
    path = holdout_path(settings.data_dir)
    bar_count: int | None = None
    if path.is_file():
        meta = inspect_holdout_file(path)
        bar_count = int(meta["bar_count"])
        print(
            f"{HOLDOUT_ID} metadata: bars={bar_count} sha256={meta['raw_sha256']} "
            f"strategy_metrics_computed={meta['strategy_metrics_computed']}"
        )
    else:
        print(f"{HOLDOUT_ID} file is absent at {path}", file=sys.stderr)
    errors = holdout_ready_errors(manifest=manifest, bar_count=bar_count)
    print(f"{HOLDOUT_ID} remains locked: " + "; ".join(errors), file=sys.stderr)
    return 1


def _run_hedge_survivor_development(settings: Settings, args: argparse.Namespace) -> int:
    if args.date_from is not None or args.date_to is not None:
        print(
            "--run-hedge-survivor-development uses the complete frozen H1 cache",
            file=sys.stderr,
        )
        return 1
    symbol = (args.symbol or settings.symbol).upper()
    if symbol != "XAUUSD" or (args.timeframe and args.timeframe != "H1"):
        print("hedge-survivor development is defined on XAUUSD H1", file=sys.stderr)
        return 1

    async def _load() -> tuple[list[Candle], list[Candle]]:
        async with httpx.AsyncClient() as http:
            store = CandleStore(settings, http)
            return (
                store.load_local("XAUUSD", Timeframe.H1),
                store.load_local("XAUUSD", Timeframe.M1),
            )

    candles, m1_bars = asyncio.run(_load())
    if not candles:
        print("No local XAUUSD H1 candles", file=sys.stderr)
        return 1
    if (
        candles[0].ts.isoformat() != DEVELOPMENT_FIRST_TS
        or candles[-1].ts.isoformat() != DEVELOPMENT_LAST_TS
    ):
        print(
            "H1 cache bounds do not match the frozen development manifest: "
            f"{candles[0].ts.isoformat()}..{candles[-1].ts.isoformat()}",
            file=sys.stderr,
        )
        return 1
    windows = build_windows(["tokyo", "london", "new_york"], DEFAULT_SESSION_SPECS)
    anchors = [anchor_from_window(window) for window in windows]
    base = EngineParams.model_validate(
        settings.engine_params().model_dump() | {"timeframe_minutes": 60}
    )
    report = run_survivor_development(
        candles,
        windows,
        base,
        anchors,
        symbol="XAUUSD",
        source="local",
        m1_bars=m1_bars,
    )
    path = write_survivor_development(report, args.output_dir)
    print(
        f"Wrote hedge-survivor development to {path}: "
        f"selected={report['selected_development_candidate']}; external holdout locked"
    )
    return 0


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


def _run_s5_resolver_bias(settings: Settings, args: argparse.Namespace) -> int:
    loaded = _load_research_inputs(settings, args, study="--run-s5-resolver-bias")
    if isinstance(loaded, int):
        return loaded
    report = run_s5_resolver_bias(
        loaded.candles,
        settings.session_windows(),
        loaded.params,
        settings.session_anchors(),
        symbol=loaded.symbol,
        timeframe=loaded.timeframe,
        source="local",
        m1_bars=loaded.m1_bars,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "s5-resolver-bias.json"
    markdown_path = args.output_dir / "s5-resolver-bias.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_s5_markdown(report), encoding="utf-8")
    print(
        f"Wrote S5 resolver tiers to {json_path} and {markdown_path}: "
        f"{report['executable_tier_count']} executed, M1 coverage "
        f"{report['m1_coverage']['status']}, export calibration unverified"
    )
    return 0


def _run_s6_walk_forward(settings: Settings, args: argparse.Namespace) -> int:
    loaded = _load_research_inputs(settings, args, study="--run-s6-walk-forward")
    if isinstance(loaded, int):
        return loaded
    if len(loaded.candles) != 2000:
        print(
            "--run-s6-walk-forward frozen protocol requires exactly 2,000 M15 bars; "
            "use --date-from/--date-to for the controlled window",
            file=sys.stderr,
        )
        return 1
    report = run_s6_walk_forward(
        loaded.candles,
        settings.session_windows(),
        loaded.params,
        settings.session_anchors(),
        symbol=loaded.symbol,
        timeframe=loaded.timeframe,
        source="local",
        m1_bars=loaded.m1_bars,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "s6-nested-walk-forward.json"
    markdown_path = args.output_dir / "s6-nested-walk-forward.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_s6_markdown(report), encoding="utf-8")
    print(
        f"Wrote S6 to {json_path} and {markdown_path}: "
        f"{report['window_protocol']['fold_count']} unseen folds, "
        f"DSR={report['deflated_sharpe_ratio']['probability']}, "
        f"PBO={report['cscv']['probability_of_backtest_overfitting']}"
    )
    return 0


def _run_s7_prop_monte_carlo(settings: Settings, args: argparse.Namespace) -> int:
    loaded = _load_research_inputs(settings, args, study="--run-s7-prop-monte-carlo")
    if isinstance(loaded, int):
        return loaded
    if len(loaded.candles) != 2000:
        print(
            "--run-s7-prop-monte-carlo evidence protocol requires exactly 2,000 M15 bars; "
            "use --date-from/--date-to for the controlled window",
            file=sys.stderr,
        )
        return 1
    report = run_s7_prop_monte_carlo(
        loaded.candles,
        settings.session_windows(),
        loaded.params,
        settings.session_anchors(),
        symbol=loaded.symbol,
        timeframe=loaded.timeframe,
        source="local",
        m1_bars=loaded.m1_bars,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "s7-propguard-monte-carlo.json"
    markdown_path = args.output_dir / "s7-propguard-monte-carlo.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_s7_markdown(report), encoding="utf-8")
    print(
        f"Wrote S7 to {json_path} and {markdown_path}: seed {report['seed']}, "
        f"{report['simulation_count_per_mode']} paths for each of {len(report['modes'])} modes"
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


# Flag on the parsed Namespace -> driver, in the order main.run() tests them.
# The table lives here rather than in main.py so the driver names never have to
# leave this module: several of them would collide with the study functions
# above (`run_s1_target_hit` is both a study and its command).
#
# The two Phase 3 scorecards read committed report files rather than candles, so
# they ignore settings. Wrapped so every entry has one shape.
COMMANDS: list[tuple[str, Callable[[Settings, argparse.Namespace], int]]] = [
    ("run_phase3_gate_scorecard", lambda _s, a: _run_phase3_gate_scorecard(a)),
    ("run_phase3_post_s6_s7_scorecard", lambda _s, a: _run_phase3_post_s6_s7_scorecard(a)),
    ("run_phase3_exploratory", _run_phase3_exploratory),
    ("run_phase3_holdout", _run_phase3_holdout),
    ("run_hedge_survivor_development", _run_hedge_survivor_development),
    ("run_s8_scale_sweep", _run_s8_scale_sweep),
    ("run_s1_target_hit", _run_s1_target_hit),
    ("run_s2_break_frequency", _run_s2_break_frequency),
    ("run_s3_anchor_study", _run_s3_anchor_study),
    ("run_s4_cost_sensitivity", _run_s4_cost_sensitivity),
    ("run_s5_resolver_bias", _run_s5_resolver_bias),
    ("run_s6_walk_forward", _run_s6_walk_forward),
    ("run_s7_prop_monte_carlo", _run_s7_prop_monte_carlo),
    ("run_s9_regime_attribution", _run_s9_regime_attribution),
]
