from __future__ import annotations

import json
from pathlib import Path

import pytest

from main import parse_args, run
from models import Candle, EngineParams, Timeframe
from research.s6_walk_forward import render_s6_markdown, run_s6_walk_forward
from sessions import build_windows

FIXTURE = Path(__file__).parent / "fixtures" / "xauusd_m15.jsonl"


def _candles() -> list[Candle]:
    return [
        Candle.model_validate_json(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _params() -> EngineParams:
    return EngineParams.model_validate(
        {
            "orb_minutes": 60,
            "entry_delay_minutes": 15,
            "time_exit_mode": "max_age",
            "max_age_hours": 24,
            "one_open_per_session": False,
            "max_concurrent_structures": 0,
            "max_open_risk_pct": 0,
        }
    )


def _report():
    return run_s6_walk_forward(
        _candles(),
        build_windows(["new_york"], {}),
        _params(),
        [],
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        source="local",
        train_bars=20,
        test_bars=7,
        holdout_bars=9,
        cscv_blocks=8,
    )


@pytest.fixture(scope="module")
def report():
    return _report()


def test_candidates_include_every_required_model_parameter(report) -> None:
    assert report["candidate_count"] == 4
    assert {item["entry_mode"] for item in report["candidates"]} == {
        "hedge_pair",
        "synthetic_breakout",
        "contingent_hedge",
        "oco_bracket",
    }
    required = {
        "session_anchors",
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
    }
    assert all(required <= set(item) for item in report["candidates"])


def test_fold_grid_is_complete_and_immediately_following(report) -> None:
    assert report["window_protocol"]["fold_count"] == 4
    assert len(report["folds"]) == 4
    evaluations = {item["evaluation_id"]: item for item in report["evaluations"]}
    for fold in report["folds"]:
        assert len(fold["training_evaluation_ids"]) == 4
        assert all(
            evaluations[item]["data_role"] == "training"
            for item in fold["training_evaluation_ids"]
        )
        unseen = evaluations[fold["unseen_evaluation_id"]]
        assert unseen["data_role"] == "unseen_test"
        assert unseen["config_id"] == fold["selected_config_id"]
        assert fold["train_last_bar_ts"] < fold["test_first_bar_ts"]


def test_aggregate_contains_only_unseen_fold_evaluations(report) -> None:
    aggregate = report["aggregate_unseen"]
    assert aggregate["source_roles"] == ["unseen_test"]
    assert aggregate["fold_count"] == 4
    evaluations = {item["evaluation_id"]: item for item in report["evaluations"]}
    sources = [evaluations[item] for item in aggregate["source_evaluation_ids"]]
    assert aggregate["gross_pips"] == pytest.approx(sum(item["gross_pips"] for item in sources))
    assert aggregate["net_r"] == pytest.approx(sum(item["net_r"] for item in sources))


def test_all_evaluations_are_logged_and_cscv_is_complete(report) -> None:
    folds = 4 * 4 + 4
    final = 4 + 1
    cscv = 8 * 4
    assert report["evaluation_count"] == folds + final + cscv
    assert len(report["evaluations"]) == report["evaluation_count"]
    assert (
        len({item["evaluation_id"] for item in report["evaluations"]})
        == report["evaluation_count"]
    )
    assert report["cscv"]["split_count"] == 70
    assert len(report["cscv"]["splits"]) == 70
    assert 0 <= report["cscv"]["probability_of_backtest_overfitting"] <= 1


def test_final_holdout_is_separate_and_protocol_is_frozen(report) -> None:
    assert report["protocol_status"] == "frozen_before_holdout_access"
    holdout = report["final_selection"]["holdout"]
    assert holdout["data_role"] == "final_unseen_holdout"
    assert holdout["evaluation_id"] not in report["aggregate_unseen"]["source_evaluation_ids"]
    assert len(report["final_selection"]["training_evaluation_ids"]) == 4


def test_gross_net_pairs_m1_fallback_and_dsr_are_reported(report) -> None:
    assert report["m1_coverage"]["status"] == "absent"
    assert report["m1_coverage"]["subpath_used"] is False
    assert report["m1_coverage"]["subpath_fallback"] == "pessimistic_same_bar_no_subpath"
    for evaluation in report["evaluations"]:
        assert evaluation["gross_pips"] - evaluation["total_cost_pips"] == pytest.approx(
            evaluation["net_pips"]
        )
        assert "gross_r" in evaluation and "net_r" in evaluation
    assert "probability" in report["deflated_sharpe_ratio"]


def test_rerun_and_markdown_are_deterministic(report) -> None:
    again = _report()
    assert json.dumps(again, sort_keys=True) == json.dumps(report, sort_keys=True)
    assert render_s6_markdown(again) == render_s6_markdown(report)
    markdown = render_s6_markdown(report)
    assert "Every evaluation" in markdown
    assert "unseen-only" in markdown.lower()


def test_cli_smoke_requires_frozen_2000_bar_window(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "data" / "candles" / "XAUUSD"
    target.mkdir(parents=True)
    (target / "M15.jsonl").write_bytes(FIXTURE.read_bytes())
    (tmp_path / ".env.s6test").write_text(
        "SYMBOL=XAUUSD\nTIMEFRAME=M15\nTRADING_SESSIONS=new_york\n"
        "DATA_DIR=data\nLOGS_DIR=logs\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    args = parse_args(["--run-s6-walk-forward"])
    assert args.run_s6_walk_forward is True
    with pytest.raises(SystemExit) as exit_info:
        run(["--profile", "s6test", "--run-s6-walk-forward", "--output-dir", "out"])
    assert exit_info.value.code == 1
    assert not (tmp_path / "out" / "s6-nested-walk-forward.json").exists()
