"""S6 nested walk-forward over the four frozen incumbent entry modes."""

from __future__ import annotations

import itertools
import math
from statistics import NormalDist, mean, stdev
from typing import Any, Literal

from ..anchors import SessionAnchor
from ..cell_stats import (
    completed_structures,
    pair_cost_r,
    pair_gross_r,
    shared_cell_metrics,
)
from ..engine import ClosedBarEngine
from ..harness.fingerprint import candle_sha256
from ..models import Candle, EngineParams, EntryMode, Timeframe
from ..sessions import SessionWindow
from . import markdown
from .scale import m1_coverage

S6_MODES: tuple[EntryMode, ...] = (
    EntryMode.HEDGE_PAIR,
    EntryMode.SYNTHETIC_BREAKOUT,
    EntryMode.CONTINGENT_HEDGE,
    EntryMode.OCO_BRACKET,
)
S6_TRAIN_BARS = 800
S6_TEST_BARS = 200
S6_HOLDOUT_BARS = 400
S6_CSCV_BLOCKS = 8
S6_EXPECTED_FOLDS = 4
S6_FALLBACK = "pessimistic_same_bar_no_subpath"


def _anchor_specs(anchors: list[SessionAnchor]) -> list[str]:
    return [
        f"{anchor.name}:{anchor.tz.key}:{anchor.at.isoformat(timespec='minutes')}"
        for anchor in anchors
    ]


def _coordinate(
    mode: EntryMode, params: EngineParams, anchors: list[SessionAnchor]
) -> dict[str, Any]:
    coordinate = {
        "session_anchors": _anchor_specs(anchors),
        "entry_mode": mode.value,
        "orb_minutes": params.orb_minutes,
        "entry_delay_minutes": params.entry_delay_minutes,
        "max_age_hours": params.max_age_hours,
        "sl_mult": params.sl_mult,
        "rr": params.rr,
        "lock_mode": params.lock_mode.value,
        "lock_pips": params.lock_pips,
        "hedge_ratio_initial": params.hedge_ratio_initial,
        "hedge_ratio_staged": params.hedge_ratio_staged,
    }
    coordinate["config_id"] = "|".join(
        [
            f"mode={coordinate['entry_mode']}",
            f"anchors={','.join(coordinate['session_anchors'])}",
            f"orb={coordinate['orb_minutes']}",
            f"delay={coordinate['entry_delay_minutes']}",
            f"age={coordinate['max_age_hours']:g}",
            f"sl={coordinate['sl_mult']:g}",
            f"rr={coordinate['rr']:g}",
            f"lock={coordinate['lock_mode']}:{coordinate['lock_pips']:g}",
            f"hedge={coordinate['hedge_ratio_initial']:g}:{coordinate['hedge_ratio_staged']:g}",
        ]
    )
    return coordinate


def _evaluation(
    candles: list[Candle],
    windows: list[SessionWindow],
    params: EngineParams,
    anchors: list[SessionAnchor],
    *,
    coordinate: dict[str, Any],
    symbol: str,
    timeframe: Timeframe,
    source: Literal["local", "ctrader"],
    m1_bars: list[Candle],
    evaluation_id: str,
    phase: str,
    data_role: str,
    fold: int | None = None,
    block: int | None = None,
) -> dict[str, Any]:
    cell_params = EngineParams.model_validate(
        params.model_dump()
        | {
            "entry_mode": coordinate["entry_mode"],
            "orb_minutes": coordinate["orb_minutes"],
            "entry_delay_minutes": coordinate["entry_delay_minutes"],
            "max_age_hours": coordinate["max_age_hours"],
            "sl_mult": coordinate["sl_mult"],
            "rr": coordinate["rr"],
            "lock_mode": coordinate["lock_mode"],
            "lock_pips": coordinate["lock_pips"],
            "hedge_ratio_initial": coordinate["hedge_ratio_initial"],
            "hedge_ratio_staged": coordinate["hedge_ratio_staged"],
        }
    )
    engine = ClosedBarEngine(windows, cell_params, anchors, m1_bars)
    engine.run(candles)
    report = engine.report(symbol, timeframe, source).model_copy(update={"bar_count": len(candles)})
    completed = completed_structures(engine, report)
    metrics = shared_cell_metrics(engine, report, completed)
    pairs = {pair.id: pair for pair in engine.pairs}
    returns: list[dict[str, Any]] = []
    session_net_r: dict[str, float] = {}
    for result in report.trade_pairs:
        if result.status != "closed":
            continue
        pair = pairs[result.id]
        gross_r = pair_gross_r(result, pair, cell_params)
        net_r = gross_r - pair_cost_r(result, pair, cell_params)
        gross_pips = float(result.gross_pnl_pips or 0.0)
        net_pips = float(result.net_pnl_pips or 0.0)
        returns.append(
            {
                "structure_id": result.id,
                "session": result.session,
                "gross_pips": gross_pips,
                "net_pips": net_pips,
                "gross_r": gross_r,
                "net_r": net_r,
            }
        )
        session_net_r[result.session] = session_net_r.get(result.session, 0.0) + net_r
    return {
        "evaluation_id": evaluation_id,
        "phase": phase,
        "data_role": data_role,
        "fold": fold,
        "cscv_block": block,
        "config_id": coordinate["config_id"],
        "coordinate": coordinate,
        "bar_count": len(candles),
        "first_bar_ts": candles[0].ts.isoformat(),
        "last_bar_ts": candles[-1].ts.isoformat(),
        "candle_set_sha256": candle_sha256(candles),
        "completed_structures": int(metrics["completed_structures"]),
        "gross_pips": float(metrics["gross_pips"]),
        "net_pips": float(metrics["net_pips"]),
        "gross_r": float(metrics["gross_r"]),
        "net_r": float(metrics["net_r"]),
        "gross_expectancy_pips": metrics["gross_expectancy_pips"],
        "net_expectancy_pips": metrics["net_expectancy_pips"],
        "gross_expectancy_r": metrics["gross_expectancy_r"],
        "net_expectancy_r": metrics["net_expectancy_r"],
        "execution_cost_pips": float(metrics["execution_cost_pips"]),
        "financing_cost_pips": float(metrics["financing_cost_pips"]),
        "total_cost_pips": float(metrics["total_cost_pips"]),
        "session_net_r": session_net_r,
        "structure_returns": returns,
    }


def _selection_key(evaluation: dict[str, Any]) -> tuple[float, str]:
    score = evaluation["net_expectancy_r"]
    # min() with a negated score makes the lexicographically smallest ID the stable tie-break.
    return (-(float(score) if score is not None else -math.inf), evaluation["config_id"])


def _aggregate_unseen(evaluations: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [item for evaluation in evaluations for item in evaluation["structure_returns"]]
    completed = len(returns)
    return {
        "source_evaluation_ids": [evaluation["evaluation_id"] for evaluation in evaluations],
        "source_roles": sorted({evaluation["data_role"] for evaluation in evaluations}),
        "fold_count": len(evaluations),
        "completed_structures": completed,
        "gross_pips": sum(evaluation["gross_pips"] for evaluation in evaluations),
        "net_pips": sum(evaluation["net_pips"] for evaluation in evaluations),
        "gross_r": sum(evaluation["gross_r"] for evaluation in evaluations),
        "net_r": sum(evaluation["net_r"] for evaluation in evaluations),
        "gross_expectancy_pips": (
            sum(item["gross_pips"] for item in returns) / completed if completed else None
        ),
        "net_expectancy_pips": (
            sum(item["net_pips"] for item in returns) / completed if completed else None
        ),
        "gross_expectancy_r": (
            sum(item["gross_r"] for item in returns) / completed if completed else None
        ),
        "net_expectancy_r": (
            sum(item["net_r"] for item in returns) / completed if completed else None
        ),
        "structure_returns": returns,
    }


def deflated_sharpe_ratio(values: list[float], trials: int) -> dict[str, Any]:
    """Bailey/Lopez de Prado DSR using unannualized structure net-R observations."""
    formula = (
        "DSR=Phi((SR-SR*)/sqrt((1-skew*SR+((kurtosis-1)/4)*SR^2)/(n-1))); "
        "SR* uses the expected maximum of N independent normal trials"
    )
    if len(values) < 3 or len(set(values)) < 2:
        return {
            "status": "not_computable",
            "reason": "at least three non-constant unseen structure returns are required",
            "observations": len(values),
            "trials": trials,
            "formula": formula,
            "sharpe": None,
            "expected_max_sharpe": None,
            "probability": None,
        }
    avg = mean(values)
    sigma = stdev(values)
    sharpe = avg / sigma
    centered = [value - avg for value in values]
    m2 = sum(value**2 for value in centered) / len(values)
    skew = sum(value**3 for value in centered) / len(values) / (m2**1.5)
    kurtosis = sum(value**4 for value in centered) / len(values) / (m2**2)
    normal = NormalDist()
    euler_gamma = 0.5772156649015329
    expected_max = 0.0
    if trials > 1:
        expected_max = (1 - euler_gamma) * normal.inv_cdf(1 - 1 / trials)
        expected_max += euler_gamma * normal.inv_cdf(1 - 1 / (trials * math.e))
    variance = (1 - skew * sharpe + ((kurtosis - 1) / 4) * sharpe**2) / (len(values) - 1)
    probability = normal.cdf((sharpe - expected_max) / math.sqrt(max(variance, 1e-15)))
    return {
        "status": "computed",
        "observations": len(values),
        "trials": trials,
        "formula": formula,
        "mean_net_r": avg,
        "sample_std_net_r": sigma,
        "skew": skew,
        "pearson_kurtosis": kurtosis,
        "sharpe": sharpe,
        "expected_max_sharpe": expected_max,
        "sharpe_variance": variance,
        "probability": probability,
    }


def _cscv(
    matrix: dict[str, list[float]], config_ids: list[str], block_count: int
) -> dict[str, Any]:
    half = block_count // 2
    splits: list[dict[str, Any]] = []
    combinations = itertools.combinations(range(block_count), half)
    for split_index, train_blocks_tuple in enumerate(combinations):
        train_blocks = set(train_blocks_tuple)
        test_blocks = [index for index in range(block_count) if index not in train_blocks]
        train_scores = {
            config_id: mean(matrix[config_id][index] for index in train_blocks)
            for config_id in config_ids
        }
        selected = min(config_ids, key=lambda config_id: (-train_scores[config_id], config_id))
        test_scores = {
            config_id: mean(matrix[config_id][index] for index in test_blocks)
            for config_id in config_ids
        }
        ordered = sorted(config_ids, key=lambda config_id: (-test_scores[config_id], config_id))
        rank = ordered.index(selected) + 1
        percentile = (len(config_ids) - rank + 0.5) / len(config_ids)
        logit = math.log(percentile / (1 - percentile))
        splits.append(
            {
                "split_index": split_index,
                "training_blocks": sorted(train_blocks),
                "test_blocks": test_blocks,
                "selected_config_id": selected,
                "training_score_net_r_per_block": train_scores[selected],
                "test_score_net_r_per_block": test_scores[selected],
                "test_rank": rank,
                "test_percentile": percentile,
                "logit": logit,
                "overfit": logit <= 0,
            }
        )
    return {
        "method": "CSCV",
        "formula": "PBO = fraction of CSCV splits where the in-sample winner ranks in the "
        "bottom half out of sample (logit <= 0)",
        "block_count": block_count,
        "split_count": len(splits),
        "configuration_count": len(config_ids),
        "probability_of_backtest_overfitting": (
            sum(split_["overfit"] for split_ in splits) / len(splits) if splits else None
        ),
        "splits": splits,
    }


def run_s6_walk_forward(
    candles: list[Candle],
    windows: list[SessionWindow],
    params: EngineParams,
    anchors: list[SessionAnchor],
    *,
    symbol: str,
    timeframe: Timeframe,
    source: Literal["local", "ctrader"],
    m1_bars: list[Candle] | None = None,
    train_bars: int = S6_TRAIN_BARS,
    test_bars: int = S6_TEST_BARS,
    holdout_bars: int = S6_HOLDOUT_BARS,
    cscv_blocks: int = S6_CSCV_BLOCKS,
) -> dict[str, Any]:
    """Execute the frozen rolling protocol and keep train results out of unseen aggregates."""
    if min(train_bars, test_bars, holdout_bars) <= 0:
        raise ValueError("S6 window sizes must be positive")
    if cscv_blocks < 2 or cscv_blocks % 2:
        raise ValueError("S6 CSCV requires an even block count of at least two")
    pre_holdout_count = len(candles) - holdout_bars
    if pre_holdout_count <= train_bars:
        raise ValueError("S6 requires training, rolling test, and final holdout bars")
    if (pre_holdout_count - train_bars) % test_bars:
        raise ValueError("S6 pre-holdout remainder must be divisible by test_bars")
    if pre_holdout_count % cscv_blocks:
        raise ValueError("S6 pre-holdout bars must divide evenly into CSCV blocks")
    fold_count = (pre_holdout_count - train_bars) // test_bars

    coverage = m1_coverage(candles, m1_bars or [], params)
    subpath_bars = (m1_bars or []) if coverage.status == "complete" else []
    coordinates = [_coordinate(mode, params, anchors) for mode in S6_MODES]
    config_ids = [coordinate["config_id"] for coordinate in coordinates]
    evaluations: list[dict[str, Any]] = []
    folds: list[dict[str, Any]] = []
    unseen_evaluations: list[dict[str, Any]] = []

    for fold in range(fold_count):
        test_start = train_bars + fold * test_bars
        train_slice = candles[test_start - train_bars : test_start]
        test_slice = candles[test_start : test_start + test_bars]
        training: list[dict[str, Any]] = []
        for candidate_index, coordinate in enumerate(coordinates):
            evaluation = _evaluation(
                train_slice,
                windows,
                params,
                anchors,
                coordinate=coordinate,
                symbol=symbol,
                timeframe=timeframe,
                source=source,
                m1_bars=subpath_bars,
                evaluation_id=f"fold-{fold}-train-config-{candidate_index}",
                phase="rolling_fold",
                data_role="training",
                fold=fold,
            )
            evaluations.append(evaluation)
            training.append(evaluation)
        selected = min(training, key=_selection_key)
        coordinate = next(
            item for item in coordinates if item["config_id"] == selected["config_id"]
        )
        unseen = _evaluation(
            test_slice,
            windows,
            params,
            anchors,
            coordinate=coordinate,
            symbol=symbol,
            timeframe=timeframe,
            source=source,
            m1_bars=subpath_bars,
            evaluation_id=f"fold-{fold}-unseen-test",
            phase="rolling_fold",
            data_role="unseen_test",
            fold=fold,
        )
        evaluations.append(unseen)
        unseen_evaluations.append(unseen)
        folds.append(
            {
                "fold": fold,
                "training_evaluation_ids": [item["evaluation_id"] for item in training],
                "selected_config_id": selected["config_id"],
                "selection_score_net_expectancy_r": selected["net_expectancy_r"],
                "unseen_evaluation_id": unseen["evaluation_id"],
                "train_first_bar_ts": train_slice[0].ts.isoformat(),
                "train_last_bar_ts": train_slice[-1].ts.isoformat(),
                "test_first_bar_ts": test_slice[0].ts.isoformat(),
                "test_last_bar_ts": test_slice[-1].ts.isoformat(),
                "unseen_gross_pips": unseen["gross_pips"],
                "unseen_net_pips": unseen["net_pips"],
                "unseen_gross_r": unseen["gross_r"],
                "unseen_net_r": unseen["net_r"],
                "unseen_net_expectancy_r": unseen["net_expectancy_r"],
            }
        )

    pre_holdout = candles[:pre_holdout_count]
    holdout = candles[pre_holdout_count:]
    final_training: list[dict[str, Any]] = []
    for candidate_index, coordinate in enumerate(coordinates):
        evaluation = _evaluation(
            pre_holdout,
            windows,
            params,
            anchors,
            coordinate=coordinate,
            symbol=symbol,
            timeframe=timeframe,
            source=source,
            m1_bars=subpath_bars,
            evaluation_id=f"final-selection-train-config-{candidate_index}",
            phase="final_selection",
            data_role="pre_holdout_training",
        )
        evaluations.append(evaluation)
        final_training.append(evaluation)
    final_selected = min(final_training, key=_selection_key)
    final_coordinate = next(
        item for item in coordinates if item["config_id"] == final_selected["config_id"]
    )
    final_holdout = _evaluation(
        holdout,
        windows,
        params,
        anchors,
        coordinate=final_coordinate,
        symbol=symbol,
        timeframe=timeframe,
        source=source,
        m1_bars=subpath_bars,
        evaluation_id="final-holdout-unseen",
        phase="final_holdout",
        data_role="final_unseen_holdout",
    )
    evaluations.append(final_holdout)

    block_size = pre_holdout_count // cscv_blocks
    matrix = {config_id: [] for config_id in config_ids}
    for block in range(cscv_blocks):
        block_slice = pre_holdout[block * block_size : (block + 1) * block_size]
        for candidate_index, coordinate in enumerate(coordinates):
            evaluation = _evaluation(
                block_slice,
                windows,
                params,
                anchors,
                coordinate=coordinate,
                symbol=symbol,
                timeframe=timeframe,
                source=source,
                m1_bars=subpath_bars,
                evaluation_id=f"cscv-block-{block}-config-{candidate_index}",
                phase="cscv",
                data_role="pre_holdout_cscv_block",
                block=block,
            )
            evaluations.append(evaluation)
            matrix[coordinate["config_id"]].append(evaluation["net_r"])

    aggregate = _aggregate_unseen(unseen_evaluations)
    dsr = deflated_sharpe_ratio(
        [item["net_r"] for item in aggregate["structure_returns"]], len(coordinates)
    )
    shared = params.model_dump(mode="json")
    for field in (
        "entry_mode",
        "orb_minutes",
        "entry_delay_minutes",
        "max_age_hours",
        "sl_mult",
        "rr",
        "lock_mode",
        "lock_pips",
        "hedge_ratio_initial",
        "hedge_ratio_staged",
    ):
        shared.pop(field, None)
    return {
        "study": "s6_nested_walk_forward",
        "protocol_status": "frozen_before_holdout_access",
        "protocol_spec": (
            "session-hedging-improvement-spec-v3.md#s6-protocol-frozen-before-holdout-access"
        ),
        "selection_rule": (
            "greatest training net_expectancy_r; stable config_id ascending tie-break"
        ),
        "symbol": symbol,
        "timeframe": timeframe.value,
        "source": source,
        "bar_count": len(candles),
        "first_bar_ts": candles[0].ts.isoformat(),
        "last_bar_ts": candles[-1].ts.isoformat(),
        "candle_set_sha256": candle_sha256(candles),
        "shared_params": shared,
        "m1_coverage": coverage.model_dump(mode="json"),
        "window_protocol": {
            "train_bars": train_bars,
            "test_bars": test_bars,
            "rolling_step_bars": test_bars,
            "fold_count": fold_count,
            "pre_holdout_bars": pre_holdout_count,
            "final_holdout_bars": holdout_bars,
            "cscv_blocks": cscv_blocks,
            "cscv_block_bars": block_size,
        },
        "candidate_count": len(coordinates),
        "candidates": coordinates,
        "folds": folds,
        "aggregate_unseen": aggregate,
        "final_selection": {
            "training_evaluation_ids": [item["evaluation_id"] for item in final_training],
            "selected_config_id": final_selected["config_id"],
            "selection_score_net_expectancy_r": final_selected["net_expectancy_r"],
            "holdout_evaluation_id": final_holdout["evaluation_id"],
            "holdout": final_holdout,
        },
        "deflated_sharpe_ratio": dsr,
        "cscv": _cscv(matrix, config_ids, cscv_blocks),
        "evaluation_count": len(evaluations),
        "evaluations": evaluations,
        "data_sufficiency": {
            "harness_verified": True,
            "strategy_selection_supported": False,
            "edge_claim_supported": False,
            "reason": "2,000 M15 bars cover roughly 30 days of one symbol.",
            "needed": "Multiple years of contiguous M15 with covering M1 and measured broker "
            "costs spanning varied trend and volatility regimes.",
        },
    }


def render_s6_markdown(report: dict[str, Any]) -> str:
    aggregate = report["aggregate_unseen"]
    holdout = report["final_selection"]["holdout"]
    dsr = report["deflated_sharpe_ratio"]
    cscv = report["cscv"]
    coverage = report["m1_coverage"]
    lines = [
        "# S6 nested walk-forward",
        "",
        "The protocol was frozen in the specification before final-holdout access. The §9 "
        "scorecard blocked redesign, so the only candidates are the four incumbent entry modes; "
        "all other named model parameters remain explicit singleton axes inside every coordinate.",
        "",
        "## Run identity and limits",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Symbol / timeframe | {report['symbol']} / {report['timeframe']} |",
        f"| Bars | {report['bar_count']} |",
        f"| Bounds | {report['first_bar_ts']} to {report['last_bar_ts']} |",
        f"| Fingerprint | `{report['candle_set_sha256']}` |",
        f"| Candidates | {report['candidate_count']} |",
        f"| Rolling folds | {report['window_protocol']['fold_count']} |",
        f"| Train / test / holdout bars | {report['window_protocol']['train_bars']} / "
        f"{report['window_protocol']['test_bars']} / "
        f"{report['window_protocol']['final_holdout_bars']} |",
        f"| M1 coverage | {coverage['status']}: {coverage['covered_parent_bars']} / "
        f"{coverage['total_parent_bars']} ({coverage['covered_parent_fraction']:.2%}) |",
        f"| Uniform fallback | `{coverage['subpath_fallback'] or 'none'}` |",
        "",
        "The 2,000-bar M15 cache covers roughly 30 days of one symbol. It verifies the harness; "
        "it cannot select a strategy or establish an edge. Multiple years of contiguous M15 with "
        "covering M1 and measured broker costs across varied regimes are required. Partial M1 "
        "chronology is not mixed; the full run uses "
        f"`{coverage['subpath_fallback'] or S6_FALLBACK}`.",
        "",
        "## Candidate coordinates",
        "",
    ]
    candidate_rows = [
        [
            str(index),
            item["entry_mode"],
            ", ".join(item["session_anchors"]),
            str(item["orb_minutes"]),
            str(item["entry_delay_minutes"]),
            f"{item['max_age_hours']:g}",
            f"{item['sl_mult']:g}",
            f"{item['rr']:g}",
            f"{item['lock_mode']}:{item['lock_pips']:g}",
            f"{item['hedge_ratio_initial']:g}/{item['hedge_ratio_staged']:g}",
        ]
        for index, item in enumerate(report["candidates"])
    ]
    lines += markdown.table(
        ["#", "Mode", "Anchors", "ORB", "Delay", "MaxAge", "SL", "RR", "Lock", "Hedge ratios"],
        candidate_rows,
        align_right_from=3,
    )
    lines += ["## Per-fold unseen results", ""]
    fold_rows = [
        [
            str(fold["fold"]),
            fold["selected_config_id"].split("|")[0].removeprefix("mode="),
            markdown.num(fold["selection_score_net_expectancy_r"], 4),
            markdown.num(fold["unseen_gross_pips"]),
            markdown.num(fold["unseen_net_pips"]),
            markdown.num(fold["unseen_gross_r"], 4),
            markdown.num(fold["unseen_net_r"], 4),
            markdown.num(fold["unseen_net_expectancy_r"], 4),
        ]
        for fold in report["folds"]
    ]
    lines += markdown.table(
        [
            "Fold",
            "Selected mode",
            "Train net exp R",
            "Unseen gross pips",
            "Unseen net pips",
            "Unseen gross R",
            "Unseen net R",
            "Unseen net exp R",
        ],
        fold_rows,
        align_right_from=2,
    )
    lines += [
        "## Unseen-only aggregate and final holdout",
        "",
        "| Result | Completed | Gross pips | Net pips | Gross R | Net R | "
        "Gross exp R | Net exp R |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
        f"| Four rolling tests | {aggregate['completed_structures']} | "
        f"{aggregate['gross_pips']:.2f} | {aggregate['net_pips']:.2f} | "
        f"{aggregate['gross_r']:.4f} | {aggregate['net_r']:.4f} | "
        f"{markdown.num(aggregate['gross_expectancy_r'], 4)} | "
        f"{markdown.num(aggregate['net_expectancy_r'], 4)} |",
        f"| Final untouched holdout | {holdout['completed_structures']} | "
        f"{holdout['gross_pips']:.2f} | {holdout['net_pips']:.2f} | "
        f"{holdout['gross_r']:.4f} | {holdout['net_r']:.4f} | "
        f"{markdown.num(holdout['gross_expectancy_r'], 4)} | "
        f"{markdown.num(holdout['net_expectancy_r'], 4)} |",
        "",
        "The rolling aggregate lists only evaluation IDs whose role is `unseen_test`; no training "
        "or CSCV block result is included. The final holdout is reported separately.",
        "",
        "## Deflated Sharpe and CSCV probability of backtest overfitting",
        "",
        "| Statistic | Value |",
        "|---|---:|",
        f"| DSR status | {dsr['status']} |",
        f"| Unseen structure observations | {dsr['observations']} |",
        f"| Raw Sharpe | {markdown.num(dsr.get('sharpe'), 6)} |",
        f"| Expected max Sharpe ({dsr['trials']} trials) | "
        f"{markdown.num(dsr.get('expected_max_sharpe'), 6)} |",
        f"| Deflated Sharpe probability | {markdown.pct(dsr.get('probability'), 4)} |",
        f"| CSCV blocks / splits | {cscv['block_count']} / {cscv['split_count']} |",
        f"| Probability of backtest overfitting | "
        f"{markdown.pct(cscv['probability_of_backtest_overfitting'], 4)} |",
        "",
        f"DSR formula: `{dsr['formula']}`.",
        "",
        f"CSCV formula: `{cscv['formula']}`. Every CSCV split and every configuration evaluation "
        "is present in the JSON artifact; no losing cell or fold is omitted.",
        "",
        "## Every evaluation",
        "",
    ]
    evaluation_rows = [
        [
            item["evaluation_id"],
            item["phase"],
            item["data_role"],
            item["coordinate"]["entry_mode"],
            str(item["bar_count"]),
            str(item["completed_structures"]),
            markdown.num(item["gross_pips"]),
            markdown.num(item["net_pips"]),
            markdown.num(item["gross_r"], 4),
            markdown.num(item["net_r"], 4),
            markdown.num(item["gross_expectancy_r"], 4),
            markdown.num(item["net_expectancy_r"], 4),
        ]
        for item in report["evaluations"]
    ]
    lines += markdown.table(
        [
            "Evaluation",
            "Phase",
            "Role",
            "Mode",
            "Bars",
            "Completed",
            "Gross pips",
            "Net pips",
            "Gross R",
            "Net R",
            "Gross exp R",
            "Net exp R",
        ],
        evaluation_rows,
        align_right_from=4,
    )
    return "\n".join(lines).rstrip() + "\n"
