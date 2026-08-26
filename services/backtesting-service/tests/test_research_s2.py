from __future__ import annotations

import json
from pathlib import Path

import pytest

from backtesting_service.anchors import build_anchors
from backtesting_service.main import run
from backtesting_service.models import Candle, EngineParams, Timeframe
from backtesting_service.research.s2_break_frequency import (
    S2_HORIZON_HOURS,
    S2_MODES,
    render_s2_markdown,
    run_s2_break_frequency,
)
from backtesting_service.sessions import build_windows

FIXTURE = Path(__file__).parent / "fixtures" / "xauusd_m15.jsonl"
SESSIONS = ["tokyo", "london", "new_york"]
CLASSES = (
    "no_break",
    "single_break_up",
    "single_break_down",
    "double_break_up_first",
    "double_break_down_first",
    "ambiguous_same_bar",
)


def _candles() -> list[Candle]:
    return [
        Candle.model_validate_json(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _params(**overrides: object) -> EngineParams:
    return EngineParams.model_validate(
        {
            "cost_model": "per_session",
            "spread_pips_per_side": 1,
            "one_open_per_session": False,
            "max_concurrent_structures": 0,
            "max_open_risk_pct": 0,
            "time_exit_mode": "max_age",
            "max_age_hours": 24,
        }
        | overrides
    )


def _report():
    return run_s2_break_frequency(
        _candles(),
        build_windows(SESSIONS, {}),
        _params(),
        build_anchors(SESSIONS),
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        source="local",
    )


@pytest.fixture(scope="module")
def s2():
    return _report()


def test_every_episode_is_classified_at_every_horizon(s2) -> None:
    assert s2.horizon_hours == list(S2_HORIZON_HOURS)
    per_horizon = {
        horizon: [item for item in s2.episodes if item.horizon_hours == horizon]
        for horizon in S2_HORIZON_HOURS
    }
    counts = {len(rows) for rows in per_horizon.values()}
    assert len(counts) == 1
    assert s2.episodes_total == counts.pop() + s2.episodes_without_forward_bars
    for item in s2.episodes:
        assert item.classification in CLASSES


def test_break_classes_are_mutually_exclusive_and_exhaustive(s2) -> None:
    for cell in s2.cells:
        total = (
            cell.no_break
            + cell.single_break_up
            + cell.single_break_down
            + cell.double_break_up_first
            + cell.double_break_down_first
            + cell.ambiguous_same_bar
        )
        assert total == cell.n
        if cell.n:
            single = (cell.single_break_up + cell.single_break_down) / cell.n
            double = (
                cell.double_break_up_first + cell.double_break_down_first + cell.ambiguous_same_bar
            ) / cell.n
            assert cell.single_break_rate == pytest.approx(single)
            assert cell.double_break_rate == pytest.approx(double)
            assert single + double + (cell.no_break_rate or 0) == pytest.approx(1.0)


def test_classification_matches_the_break_timestamps(s2) -> None:
    for item in s2.episodes:
        if item.classification == "no_break":
            assert item.first_break_side == "none"
            assert item.first_break_hours is None
            assert item.opposite_break_hours is None
        elif item.classification.startswith("single_break"):
            assert item.first_break_hours is not None
            assert item.opposite_break_hours is None
            assert item.classification.endswith(item.first_break_side)
        elif item.classification.startswith("double_break"):
            assert item.first_break_hours is not None
            assert item.opposite_break_hours is not None
            assert item.opposite_break_hours >= item.first_break_hours
        else:
            assert item.classification == "ambiguous_same_bar"
            assert item.first_break_side == "both"


def test_double_breaks_only_grow_with_the_horizon(s2) -> None:
    by_key = {(cell.group_kind, cell.group_key, cell.horizon_hours): cell for cell in s2.cells}
    for (kind, key, horizon), cell in by_key.items():
        longer = [value for value in S2_HORIZON_HOURS if value > horizon]
        if not longer:
            continue
        nxt = by_key[(kind, key, min(longer))]
        doubles = cell.double_break_up_first + cell.double_break_down_first
        next_doubles = nxt.double_break_up_first + nxt.double_break_down_first
        assert next_doubles >= doubles
        assert nxt.no_break <= cell.no_break


def test_mode_companions_cover_all_four_modes_with_paired_gross_and_net(s2) -> None:
    assert [item.entry_mode for item in s2.mode_companions] == list(S2_MODES)
    for item in s2.mode_companions:
        assert item.gross_r >= item.net_r
        assert item.whipsaw_structures <= item.completed_structures
        assert (
            item.tp_structures
            + item.lock_structures
            + item.breakeven_structures
            + item.whipsaw_structures
            + item.time_exit_structures
            == item.completed_structures
        )
        if item.whipsaw_rate is not None:
            assert item.whipsaw_ci_low <= item.whipsaw_rate <= item.whipsaw_ci_high
        assert "triggered structures" in item.false_break_definition


def test_rerun_is_byte_identical(s2) -> None:
    assert _report().model_dump_json() == s2.model_dump_json()


def test_markdown_names_the_ambiguous_class_and_prints_every_cell(s2) -> None:
    markdown = render_s2_markdown(s2)

    assert "ambiguous_same_bar" in markdown
    assert s2.candle_set_sha256 in markdown
    assert "opening_range_close" in markdown
    for cell in s2.cells:
        assert f"| {cell.group_kind} | {cell.group_key} | {cell.horizon_hours:g} |" in markdown


def test_cli_writes_the_s2_artifacts(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "data" / "candles" / "XAUUSD").mkdir(parents=True)
    (tmp_path / "data" / "candles" / "XAUUSD" / "M15.jsonl").write_text(
        FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / ".env.s2test").write_text(
        "SYMBOL=XAUUSD\nTIMEFRAME=M15\nTRADING_SESSIONS=new_york\nDATA_DIR=data\nLOGS_DIR=logs\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        run(["--profile", "s2test", "--run-s2-break-frequency", "--output-dir", "out"])

    assert exit_info.value.code == 0
    written = json.loads((tmp_path / "out" / "s2-break-frequency.json").read_text())
    assert written["study"] == "s2_break_frequency"
    assert written["walk_starts_at"] == "opening_range_close"
    assert (tmp_path / "out" / "s2-break-frequency.md").exists()
