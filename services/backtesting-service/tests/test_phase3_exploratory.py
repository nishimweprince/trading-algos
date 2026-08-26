"""§8.0 exploratory protocol units: cache lock, budget, folds, holdout, selection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backtesting_service.models import Candle, EngineParams
from backtesting_service.research.phase3_coordinates import PHASE3_COORDINATES, SHARED_BASE
from backtesting_service.research.phase3_exploratory import (
    DEVELOPMENT_BAR_COUNT,
    DEVELOPMENT_RAW_SHA256,
    EVAL_CAP,
    TRAIN0,
    DevelopmentCacheError,
    EvalBudget,
    EvalBudgetExceeded,
    bootstrap_lower_bound,
    fold_windows,
    is_eligible,
    refuse_holdout_evaluation,
    render_phase3_exploratory_markdown,
    run_phase3_exploratory,
    select_coordinate,
    verify_development_cache,
)
from backtesting_service.research.phase3_holdout import HoldoutLockedError, holdout_unlock_errors
from backtesting_service.sessions import build_windows


def _bar(ts: datetime) -> Candle:
    return Candle(
        ts=ts,
        open=2000,
        high=2001,
        low=1999,
        close=2000.5,
        volume=1.0,
        provider="test",
        source_instrument="XAUUSD",
    )


def test_frozen_folds_cover_the_9998_bar_snapshot() -> None:
    windows = fold_windows()
    assert len(windows) == 8
    assert windows[0][0] == slice(0, TRAIN0)
    assert windows[0][1] == slice(TRAIN0, TRAIN0 + 500)
    assert windows[7][0] == slice(0, 9498)
    assert windows[7][1] == slice(9498, DEVELOPMENT_BAR_COUNT)
    budget = 104 * 8 + 8 + 104 + 8 + 2
    assert budget == EVAL_CAP == 954


def test_eval_budget_fails_closed() -> None:
    budget = EvalBudget(cap=2)
    budget.consume("a")
    budget.consume("b")
    with pytest.raises(EvalBudgetExceeded, match="2"):
        budget.consume("c")


def test_bootstrap_lower_bound_is_deterministic() -> None:
    values = [0.1, -0.2, 0.3, -0.4, 0.5]
    first = bootstrap_lower_bound(values, resamples=200, seed=20260820)
    second = bootstrap_lower_bound(values, resamples=200, seed=20260820)
    other = bootstrap_lower_bound(values, resamples=200, seed=20260821)
    assert first == second
    assert first is not None and other is not None
    assert first != other


def test_eligibility_requires_structures_and_each_session() -> None:
    evaluation = {
        "completed_structures": 20,
        "structure_returns": (
            [{"session": "tokyo", "net_r": 0.1}] * 3
            + [{"session": "london", "net_r": 0.1}] * 3
            + [{"session": "new_york", "net_r": 0.1}] * 14
        ),
    }
    assert is_eligible(evaluation)
    evaluation["completed_structures"] = 19
    assert not is_eligible(evaluation)


def test_selection_prefers_higher_bootstrap_lower_bound() -> None:
    weak = {
        "coordinate_id": "b",
        "lane": "incumbent",
        "completed_structures": 20,
        "net_max_drawdown_r": 1.0,
        "cost_side_equivalents": 10,
        "structure_returns": (
            [{"session": "tokyo", "net_r": -1.0}] * 7
            + [{"session": "london", "net_r": -1.0}] * 7
            + [{"session": "new_york", "net_r": -1.0}] * 6
        ),
    }
    strong = {
        "coordinate_id": "a",
        "lane": "incumbent",
        "completed_structures": 20,
        "net_max_drawdown_r": 5.0,
        "cost_side_equivalents": 40,
        "structure_returns": (
            [{"session": "tokyo", "net_r": 1.0}] * 7
            + [{"session": "london", "net_r": 1.0}] * 7
            + [{"session": "new_york", "net_r": 1.0}] * 6
        ),
    }
    selected = select_coordinate([weak, strong], seed=20260820, resamples=50)
    assert selected is not None
    assert selected["coordinate_id"] == "a"


def test_holdout_stays_locked_without_the_complete_manifest() -> None:
    with pytest.raises(HoldoutLockedError, match="locked"):
        refuse_holdout_evaluation(None)
    errors = holdout_unlock_errors({"protocol_commit": "deadbeef"})
    assert "missing candidate_list_hash" in errors
    assert any("27a85ef" in item for item in errors)


def test_verify_development_cache_rejects_a_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "M15.jsonl"
    path.write_text("not-the-frozen-cache\n", encoding="utf-8")
    candles = [_bar(datetime(2026, 3, 19, 7, 45, tzinfo=UTC))]
    with pytest.raises(DevelopmentCacheError, match="SHA-256"):
        verify_development_cache(path, candles)
    assert DEVELOPMENT_RAW_SHA256.startswith("c45d540d")


def test_tiny_exploratory_run_does_not_touch_holdout() -> None:
    start = datetime(2026, 1, 14, 0, 0, tzinfo=UTC)
    candles = [_bar(start + timedelta(minutes=15 * i)) for i in range(40)]
    family = [
        {
            "id": "incumbent:hedge_pair",
            "lane": "incumbent",
            "params": dict(SHARED_BASE) | {"entry_mode": "hedge_pair"},
        }
    ]
    report = run_phase3_exploratory(
        candles,
        build_windows(["tokyo", "london", "new_york"], {}),
        EngineParams(orb_minutes=15, timeframe_minutes=15, entry_delay_minutes=15),
        [],
        coordinates=family,
        train0=20,
        test_len=10,
        folds=2,
        eval_cap=20,
        bootstrap_resamples=20,
        min_completed=1,
        min_per_session=0,
        verify_cache=False,
    )
    assert report["holdout_accessed"] is False
    assert report["holdout_status"] == "locked"
    assert report["evaluation_count"] <= 20
    assert report["coordinate_count"] == 1
    assert "No coordinate is promoted" in render_phase3_exploratory_markdown(report)
    assert PHASE3_COORDINATES[0]["id"] == "incumbent:hedge_pair"
