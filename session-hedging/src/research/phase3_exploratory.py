"""§8.0 bounded exploratory Phase 3 development protocol.

A negative result closes the tested family. A positive result is exploratory
evidence only. This harness never unlocks the prospective holdout and never
claims a §9 gate passed.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from random import Random
from typing import Any, Literal

from anchors import SessionAnchor
from cell_stats import (
    candle_sha256,
    completed_structures,
    pair_cost_r,
    pair_gross_r,
    shared_cell_metrics,
)
from engine import ClosedBarEngine
from models import Candle, EngineParams, IntrabarMode, Timeframe
from research.phase3_coordinates import (
    PHASE3_COORDINATE_COUNT,
    PHASE3_COORDINATE_SHA256,
    PHASE3_COORDINATES,
    STRESS_COST,
    apply_phase3_coordinate,
    shared_base_params,
)
from research.phase3_holdout import assert_holdout_locked
from research.s6_walk_forward import _cscv, deflated_sharpe_ratio
from research.scale import m1_coverage
from sessions import SessionWindow

DEVELOPMENT_BAR_COUNT = 9998
DEVELOPMENT_FIRST_TS = datetime(2026, 3, 19, 7, 45, tzinfo=UTC)
DEVELOPMENT_LAST_TS = datetime(2026, 8, 20, 10, 45, tzinfo=UTC)
DEVELOPMENT_RAW_SHA256 = "c45d540d1d06c00459e41d7c29fc1d8844fe599c16e03bc348ac0138eaf63fa1"
EVAL_CAP = 954
TRAIN0 = 5998
TEST_LEN = 500
FOLD_COUNT = 8
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_BASE_SEED = 20260820
FULL_DEV_SEED = 20260829
MIN_COMPLETED = 20
MIN_PER_SESSION = 3
SESSIONS = ("tokyo", "london", "new_york")
DEVELOPMENT_STEM = "phase3-exploratory-development"


class DevelopmentCacheError(ValueError):
    """The on-disk development snapshot is not the frozen §8.0 cache."""


class EvalBudgetExceeded(ValueError):
    """The frozen 954-evaluation cap would be exceeded."""


@dataclass
class EvalBudget:
    cap: int = EVAL_CAP
    used: int = 0
    labels: list[str] = field(default_factory=list)

    def consume(self, label: str) -> None:
        if self.used + 1 > self.cap:
            raise EvalBudgetExceeded(
                f"Phase 3 exploratory budget cap {self.cap} would be exceeded by {label}"
            )
        self.used += 1
        self.labels.append(label)


def raw_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_development_cache(path: Path, candles: list[Candle]) -> dict[str, str]:
    raw = raw_file_sha256(path)
    if raw != DEVELOPMENT_RAW_SHA256:
        raise DevelopmentCacheError(f"development cache SHA-256 {raw} != {DEVELOPMENT_RAW_SHA256}")
    if len(candles) != DEVELOPMENT_BAR_COUNT:
        raise DevelopmentCacheError(
            f"development cache has {len(candles)} bars, not {DEVELOPMENT_BAR_COUNT}"
        )
    first = candles[0].ts.astimezone(UTC)
    last = candles[-1].ts.astimezone(UTC)
    if first != DEVELOPMENT_FIRST_TS or last != DEVELOPMENT_LAST_TS:
        raise DevelopmentCacheError(
            f"development cache bounds {first.isoformat()}..{last.isoformat()} "
            f"!= {DEVELOPMENT_FIRST_TS.isoformat()}..{DEVELOPMENT_LAST_TS.isoformat()}"
        )
    return {
        "raw_sha256": raw,
        "canonical_sha256": candle_sha256(candles),
        "first_bar_ts": first.isoformat(),
        "last_bar_ts": last.isoformat(),
    }


def fold_windows(
    *, train0: int = TRAIN0, test_len: int = TEST_LEN, folds: int = FOLD_COUNT
) -> list[tuple[slice, slice]]:
    windows: list[tuple[slice, slice]] = []
    for fold in range(folds):
        train_end = train0 + test_len * fold
        test_end = train_end + test_len
        windows.append((slice(0, train_end), slice(train_end, test_end)))
    return windows


def bootstrap_lower_bound(
    values: list[float], *, resamples: int, seed: int, quantile: float = 0.10
) -> float | None:
    """Lower bound of a one-sided 90% bootstrap interval for the mean."""
    if not values:
        return None
    rng = Random(seed)
    n = len(values)
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(resamples))
    index = min(len(means) - 1, max(0, int(quantile * resamples)))
    return means[index]


def _session_counts(returns: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {name: 0 for name in SESSIONS}
    for item in returns:
        session = str(item["session"])
        if session in counts:
            counts[session] += 1
    return counts


def is_eligible(
    evaluation: dict[str, Any],
    *,
    min_completed: int = MIN_COMPLETED,
    min_per_session: int = MIN_PER_SESSION,
) -> bool:
    if int(evaluation["completed_structures"]) < min_completed:
        return False
    counts = _session_counts(evaluation["structure_returns"])
    return all(counts[name] >= min_per_session for name in SESSIONS)


def select_coordinate(
    evaluations: list[dict[str, Any]],
    *,
    seed: int,
    resamples: int = BOOTSTRAP_RESAMPLES,
    min_completed: int = MIN_COMPLETED,
    min_per_session: int = MIN_PER_SESSION,
) -> dict[str, Any] | None:
    eligible = [
        item
        for item in evaluations
        if is_eligible(item, min_completed=min_completed, min_per_session=min_per_session)
    ]
    if not eligible:
        return None

    def key(item: dict[str, Any]) -> tuple[float, float, float, str]:
        returns = [float(row["net_r"]) for row in item["structure_returns"]]
        lower = bootstrap_lower_bound(returns, resamples=resamples, seed=seed)
        bound = float("-inf") if lower is None else lower
        dd = float(item.get("net_max_drawdown_r") or 0.0)
        sides = float(item.get("cost_side_equivalents") or 0.0)
        return (-bound, dd, sides, str(item["coordinate_id"]))

    winner = min(eligible, key=key)
    returns = [float(row["net_r"]) for row in winner["structure_returns"]]
    return {
        "coordinate_id": winner["coordinate_id"],
        "lane": winner["lane"],
        "bootstrap_lower_net_expectancy_r": bootstrap_lower_bound(
            returns, resamples=resamples, seed=seed
        ),
        "net_max_drawdown_r": winner.get("net_max_drawdown_r"),
        "cost_side_equivalents": winner.get("cost_side_equivalents"),
        "completed_structures": winner["completed_structures"],
    }


def _session_attribution(evaluations: list[dict[str, Any]]) -> dict[str, dict[str, float | int]]:
    by_session: dict[str, dict[str, float | int]] = {
        name: {"completed_structures": 0, "net_pips": 0.0, "net_r": 0.0} for name in SESSIONS
    }
    for evaluation in evaluations:
        for row in evaluation.get("structure_returns", []):
            session = str(row["session"])
            if session not in by_session:
                continue
            by_session[session]["completed_structures"] = (
                int(by_session[session]["completed_structures"]) + 1
            )
            by_session[session]["net_pips"] = float(by_session[session]["net_pips"]) + float(
                row["net_pips"]
            )
            by_session[session]["net_r"] = float(by_session[session]["net_r"]) + float(row["net_r"])
    return by_session


def _neighbourhood(
    evaluations: list[dict[str, Any]],
    *,
    seed: int,
    resamples: int,
    selected_id: str | None,
    plateau_tol: float = 0.05,
) -> dict[str, Any]:
    ranked: list[dict[str, Any]] = []
    for item in evaluations:
        returns = [float(row["net_r"]) for row in item["structure_returns"]]
        ranked.append(
            {
                "coordinate_id": item["coordinate_id"],
                "lane": item["lane"],
                "eligible": is_eligible(item),
                "bootstrap_lower_net_expectancy_r": bootstrap_lower_bound(
                    returns, resamples=resamples, seed=seed
                ),
                "net_r": item["net_r"],
                "net_max_drawdown_r": item.get("net_max_drawdown_r"),
            }
        )
    ranked.sort(
        key=lambda row: (
            -(row["bootstrap_lower_net_expectancy_r"] or float("-inf")),
            float(row.get("net_max_drawdown_r") or 0.0),
            str(row["coordinate_id"]),
        )
    )
    winner = next((row for row in ranked if row["coordinate_id"] == selected_id), None)
    winner_bound = winner["bootstrap_lower_net_expectancy_r"] if winner else None
    plateau = 0
    if winner_bound is not None:
        plateau = sum(
            1
            for row in ranked
            if row["eligible"]
            and row["bootstrap_lower_net_expectancy_r"] is not None
            and abs(float(row["bootstrap_lower_net_expectancy_r"]) - float(winner_bound))
            <= plateau_tol
        )
    return {
        "selected_coordinate_id": selected_id,
        "plateau_tolerance_r": plateau_tol,
        "plateau_count": plateau,
        "ranked": ranked[:10],
    }


def _resolver_mode(
    candles: list[Candle], m1_bars: list[Candle], params: EngineParams
) -> tuple[IntrabarMode, list[Candle], str]:
    coverage = m1_coverage(candles, m1_bars, params)
    if coverage.status == "complete":
        return IntrabarMode.M1_CONSERVATIVE, m1_bars, "m1_conservative"
    return IntrabarMode.PESSIMISTIC, [], "pessimistic_same_bar_no_subpath"


def evaluate_coordinate(
    *,
    candles: list[Candle],
    windows: list[SessionWindow],
    anchors: list[SessionAnchor],
    base: EngineParams,
    coordinate: dict[str, Any],
    m1_bars: list[Candle],
    budget: EvalBudget,
    label: str,
    symbol: str,
    timeframe: Timeframe,
    source: Literal["local", "ctrader"],
    cost_overrides: dict[str, float] | None = None,
) -> dict[str, Any]:
    budget.consume(label)
    params = apply_phase3_coordinate(shared_base_params(), coordinate)
    if cost_overrides:
        params = EngineParams.model_validate(params.model_dump() | cost_overrides)
    mode, subpath, resolver = _resolver_mode(candles, m1_bars, params)
    params = EngineParams.model_validate(params.model_dump() | {"intrabar_mode": mode})
    engine = ClosedBarEngine(windows, params, anchors, subpath)
    engine.run(candles)
    report = engine.report(symbol, timeframe, source).model_copy(update={"bar_count": len(candles)})
    completed = completed_structures(engine, report)
    metrics = shared_cell_metrics(engine, report, completed)
    pairs = {pair.id: pair for pair in engine.pairs}
    returns: list[dict[str, Any]] = []
    for result in report.trade_pairs:
        if result.status != "closed":
            continue
        pair = pairs[result.id]
        gross_r = pair_gross_r(result, pair, params)
        returns.append(
            {
                "structure_id": result.id,
                "session": result.session,
                "gross_pips": float(result.gross_pnl_pips or 0.0),
                "net_pips": float(result.net_pnl_pips or 0.0),
                "gross_r": gross_r,
                "net_r": gross_r - pair_cost_r(result, pair, params),
            }
        )
    return {
        "evaluation_id": label,
        "coordinate_id": coordinate["id"],
        "lane": coordinate["lane"],
        "resolver": resolver,
        "bar_count": len(candles),
        "first_bar_ts": candles[0].ts.isoformat() if candles else None,
        "last_bar_ts": candles[-1].ts.isoformat() if candles else None,
        "completed_structures": int(metrics["completed_structures"]),
        "gross_pips": float(metrics["gross_pips"]),
        "net_pips": float(metrics["net_pips"]),
        "gross_r": float(metrics["gross_r"]),
        "net_r": float(metrics["net_r"]),
        "net_expectancy_r": metrics["net_expectancy_r"],
        "net_max_drawdown_r": report.net_max_drawdown_r,
        "cost_side_equivalents": report.cost_side_equivalents,
        "structure_returns": returns,
        "params": coordinate["params"],
    }


def run_phase3_exploratory(
    candles: list[Candle],
    windows: list[SessionWindow],
    params: EngineParams,
    anchors: list[SessionAnchor],
    *,
    symbol: str = "XAUUSD",
    timeframe: Timeframe = Timeframe.M15,
    source: Literal["local", "ctrader"] = "local",
    m1_bars: list[Candle] | None = None,
    cache_path: Path | None = None,
    coordinates: list[dict[str, Any]] | None = None,
    train0: int = TRAIN0,
    test_len: int = TEST_LEN,
    folds: int = FOLD_COUNT,
    eval_cap: int = EVAL_CAP,
    bootstrap_resamples: int = BOOTSTRAP_RESAMPLES,
    min_completed: int = MIN_COMPLETED,
    min_per_session: int = MIN_PER_SESSION,
    verify_cache: bool = True,
) -> dict[str, Any]:
    """Run the frozen development protocol. Holdout bars are never evaluated."""
    family = coordinates if coordinates is not None else PHASE3_COORDINATES
    if verify_cache:
        if cache_path is None:
            raise DevelopmentCacheError("development cache path is required")
        cache: dict[str, Any] = verify_development_cache(cache_path, candles)
    else:
        cache = {
            "raw_sha256": None,
            "canonical_sha256": candle_sha256(candles),
            "first_bar_ts": candles[0].ts.isoformat() if candles else None,
            "last_bar_ts": candles[-1].ts.isoformat() if candles else None,
        }
    budget = EvalBudget(cap=eval_cap)
    m1 = m1_bars or []
    fold_results: list[dict[str, Any]] = []
    for fold, (train_slice, test_slice) in enumerate(
        fold_windows(train0=train0, test_len=test_len, folds=folds)
    ):
        train_bars = candles[train_slice]
        test_bars = candles[test_slice]
        train_evals = [
            evaluate_coordinate(
                candles=train_bars,
                windows=windows,
                anchors=anchors,
                base=params,
                coordinate=coordinate,
                m1_bars=m1,
                budget=budget,
                label=f"fold{fold}:train:{coordinate['id']}",
                symbol=symbol,
                timeframe=timeframe,
                source=source,
            )
            for coordinate in family
        ]
        selected = select_coordinate(
            train_evals,
            seed=BOOTSTRAP_BASE_SEED + fold,
            resamples=bootstrap_resamples,
            min_completed=min_completed,
            min_per_session=min_per_session,
        )
        test_eval = None
        stress_eval = None
        if selected is not None:
            winner = next(item for item in family if item["id"] == selected["coordinate_id"])
            test_eval = evaluate_coordinate(
                candles=test_bars,
                windows=windows,
                anchors=anchors,
                base=params,
                coordinate=winner,
                m1_bars=m1,
                budget=budget,
                label=f"fold{fold}:test:{winner['id']}",
                symbol=symbol,
                timeframe=timeframe,
                source=source,
            )
            stress_eval = evaluate_coordinate(
                candles=test_bars,
                windows=windows,
                anchors=anchors,
                base=params,
                coordinate=winner,
                m1_bars=m1,
                budget=budget,
                label=f"fold{fold}:stress:{winner['id']}",
                symbol=symbol,
                timeframe=timeframe,
                source=source,
                cost_overrides=STRESS_COST,
            )
        fold_results.append(
            {
                "fold": fold,
                "train_bar_count": len(train_bars),
                "test_bar_count": len(test_bars),
                "train_evaluations": train_evals,
                "selected": selected,
                "unseen_test": test_eval,
                "unseen_stress": stress_eval,
            }
        )

    full_evals = [
        evaluate_coordinate(
            candles=candles,
            windows=windows,
            anchors=anchors,
            base=params,
            coordinate=coordinate,
            m1_bars=m1,
            budget=budget,
            label=f"full:{coordinate['id']}",
            symbol=symbol,
            timeframe=timeframe,
            source=source,
        )
        for coordinate in family
    ]
    full_selected = select_coordinate(
        full_evals,
        seed=FULL_DEV_SEED,
        resamples=bootstrap_resamples,
        min_completed=min_completed,
        min_per_session=min_per_session,
    )
    unseen = [fold["unseen_test"] for fold in fold_results if fold["unseen_test"] is not None]
    unseen_stress = [
        fold["unseen_stress"] for fold in fold_results if fold["unseen_stress"] is not None
    ]
    unseen_returns = [row["net_r"] for item in unseen for row in item["structure_returns"]]
    dsr = deflated_sharpe_ratio(unseen_returns, trials=len(family))
    pbo: dict[str, Any] = {
        "status": "not_computable",
        "reason": (
            "CSCV of the 104-coordinate family on disjoint unseen blocks would require "
            "832 extra test-fold evaluations and exceed the 954-evaluation cap. "
            "PBO is not claimed."
        ),
        "probability": None,
    }
    if len(unseen) >= 4 and len(unseen) % 2 == 0:
        unique_ids = list(dict.fromkeys(item["coordinate_id"] for item in unseen))
        aligned = {
            coord_id: [float(item["net_r"]) for item in unseen if item["coordinate_id"] == coord_id]
            for coord_id in unique_ids
        }
        block_len = min((len(values) for values in aligned.values()), default=0)
        if len(unique_ids) >= 2 and block_len >= 2 and block_len % 2 == 0:
            blocks = {key: values[:block_len] for key, values in aligned.items()}
            pbo = _cscv(blocks, list(blocks), block_len)
    assert_holdout_locked(manifest=None, evaluating_strategy=False)
    return {
        "study": "phase3_exploratory_development",
        "interpretation": (
            "A negative result closes the tested family and is publishable. "
            "A positive result is exploratory evidence only. No §9 gate is claimed passed."
        ),
        "protocol_commit": "27a85ef",
        "coordinate_count": len(family),
        "candidate_list_hash": PHASE3_COORDINATE_SHA256,
        "expected_coordinate_count": PHASE3_COORDINATE_COUNT,
        "development_cache": cache,
        "evaluation_count": budget.used,
        "evaluation_cap": eval_cap,
        "commission_swap_labelled": "missing",
        "holdout_accessed": False,
        "holdout_status": "locked",
        "folds": fold_results,
        "full_development": {
            "evaluations": full_evals,
            "selected": full_selected,
            "neighbourhood": _neighbourhood(
                full_evals,
                seed=FULL_DEV_SEED,
                resamples=bootstrap_resamples,
                selected_id=(full_selected["coordinate_id"] if full_selected is not None else None),
            ),
        },
        "unseen_aggregate": {
            "fold_count": len(unseen),
            "gross_pips": sum(item["gross_pips"] for item in unseen),
            "net_pips": sum(item["net_pips"] for item in unseen),
            "gross_r": sum(item["gross_r"] for item in unseen),
            "net_r": sum(item["net_r"] for item in unseen),
            "completed_structures": sum(item["completed_structures"] for item in unseen),
            "by_session": _session_attribution(unseen),
            "stress": {
                "fold_count": len(unseen_stress),
                "gross_pips": sum(item["gross_pips"] for item in unseen_stress),
                "net_pips": sum(item["net_pips"] for item in unseen_stress),
                "gross_r": sum(item["gross_r"] for item in unseen_stress),
                "net_r": sum(item["net_r"] for item in unseen_stress),
            },
        },
        "dsr": dsr,
        "pbo": pbo,
        "lane_counts": dict(Counter(item["lane"] for item in family)),
    }


def render_phase3_exploratory_markdown(report: dict[str, Any]) -> str:
    cache = report["development_cache"]
    selected = report["full_development"]["selected"]
    unseen = report["unseen_aggregate"]
    stress = unseen.get("stress") or {
        "net_pips": 0.0,
        "net_r": 0.0,
    }
    neighbourhood = report["full_development"].get("neighbourhood") or {}
    selected_id = selected["coordinate_id"] if selected else "none"
    lines = [
        "# Phase 3 exploratory development",
        "",
        report["interpretation"],
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Protocol commit | `{report['protocol_commit']}` |",
        f"| Coordinates | {report['coordinate_count']} |",
        f"| Candidate-list SHA-256 | `{report['candidate_list_hash']}` |",
        f"| Development raw SHA-256 | `{cache.get('raw_sha256')}` |",
        f"| Development canonical SHA-256 | `{cache.get('canonical_sha256')}` |",
        f"| Bars | {cache.get('first_bar_ts')} … {cache.get('last_bar_ts')} |",
        f"| Evaluations | {report['evaluation_count']} / {report['evaluation_cap']} |",
        f"| Holdout | {report['holdout_status']} (accessed={report['holdout_accessed']}) |",
        f"| Unseen net pips / R | {unseen['net_pips']:.4f} / {unseen['net_r']:.4f} |",
        f"| Unseen stress net pips / R | {stress['net_pips']:.4f} / {stress['net_r']:.4f} |",
        f"| Full-development selected | `{selected_id}` |",
        f"| Neighbourhood plateau | {neighbourhood.get('plateau_count', 0)} within "
        f"{neighbourhood.get('plateau_tolerance_r', 0)} R |",
        "",
        "## Unseen folds",
        "",
        "| Fold | Selected | Test net R | Stress net R |",
        "|---|---|---|---|",
    ]
    for fold in report["folds"]:
        fold_selected = fold["selected"]
        test = fold["unseen_test"]
        stress_fold = fold["unseen_stress"]
        lines.append(
            "| {fold} | `{sid}` | {test} | {stress} |".format(
                fold=fold["fold"],
                sid=fold_selected["coordinate_id"] if fold_selected else "none",
                test=f"{test['net_r']:.4f}" if test else "—",
                stress=f"{stress_fold['net_r']:.4f}" if stress_fold else "—",
            )
        )
    lines.extend(
        [
            "",
            "Every training evaluation and losing coordinate is retained in the JSON companion.",
            "No coordinate is promoted. Live trading is not enabled.",
            "",
        ]
    )
    return "\n".join(lines)


def write_phase3_exploratory_reports(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{DEVELOPMENT_STEM}.json"
    markdown_path = output_dir / f"{DEVELOPMENT_STEM}.md"
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    markdown_path.write_text(render_phase3_exploratory_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def refuse_holdout_evaluation(manifest: dict[str, Any] | None = None) -> None:
    """Holdout strategy evaluation is forbidden without the complete §8.0 manifest."""
    assert_holdout_locked(manifest=manifest, evaluating_strategy=True)
