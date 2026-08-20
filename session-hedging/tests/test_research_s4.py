from __future__ import annotations

import json
from pathlib import Path

import pytest

from anchors import build_anchors
from main import run
from models import Candle, EngineParams, Timeframe
from research.s4_cost_sensitivity import (
    S4_CELL_COUNT,
    S4_COMMISSION_GRID,
    S4_HEADROOM_GATE,
    S4_MODES,
    S4_SLIPPAGE_GRID,
    S4_SPREAD_GRID,
    render_s4_markdown,
    run_s4_cost_sensitivity,
)
from sessions import build_windows

FIXTURE = Path(__file__).parent / "fixtures" / "xauusd_m15.jsonl"
SESSIONS = ["tokyo", "london", "new_york"]


def _candles() -> list[Candle]:
    return [
        Candle.model_validate_json(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _params() -> EngineParams:
    return EngineParams.model_validate(
        {
            "cost_model": "per_session",
            "one_open_per_session": False,
            "max_concurrent_structures": 0,
            "max_open_risk_pct": 0,
            "time_exit_mode": "max_age",
            "max_age_hours": 24,
        }
    )


def _report():
    return run_s4_cost_sensitivity(
        _candles(),
        build_windows(SESSIONS, {}),
        _params(),
        build_anchors(SESSIONS),
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        source="local",
    )


@pytest.fixture(scope="module")
def s4():
    return _report()


def test_the_cost_grid_is_complete(s4) -> None:
    assert S4_CELL_COUNT == len(S4_MODES) * len(S4_SPREAD_GRID) * len(S4_SLIPPAGE_GRID) * len(
        S4_COMMISSION_GRID
    )
    assert s4.expected_cell_count == S4_CELL_COUNT
    assert len(s4.cells) == S4_CELL_COUNT
    coordinates = {
        (
            cell.entry_mode,
            cell.spread_pips_per_side,
            cell.slippage_pips_per_side,
            cell.commission_pips_per_side,
        )
        for cell in s4.cells
    }
    assert len(coordinates) == S4_CELL_COUNT


def test_only_the_cost_fields_and_mode_vary(s4) -> None:
    for field in (
        "entry_mode",
        "spread_pips_per_side",
        "slippage_pips_per_side",
        "commission_pips_per_side",
    ):
        assert field not in s4.shared_params


def test_cost_rises_monotonically_and_net_falls_with_it(s4) -> None:
    for mode in S4_MODES:
        for slippage in S4_SLIPPAGE_GRID:
            for commission in S4_COMMISSION_GRID:
                ladder = sorted(
                    (
                        cell
                        for cell in s4.cells
                        if cell.entry_mode is mode
                        and cell.slippage_pips_per_side == slippage
                        and cell.commission_pips_per_side == commission
                    ),
                    key=lambda cell: cell.spread_pips_per_side,
                )
                gross = {cell.gross_pips for cell in ladder}
                assert len(gross) == 1, "cost must not change the gross path"
                for earlier, later in zip(ladder, ladder[1:], strict=False):
                    assert (
                        later.configured_execution_cost_pips_per_side
                        > earlier.configured_execution_cost_pips_per_side
                    )
                    assert later.execution_cost_pips >= earlier.execution_cost_pips
                    assert later.net_pips <= earlier.net_pips


def test_headroom_gate_is_applied_exactly(s4) -> None:
    assert s4.headroom_gate == S4_HEADROOM_GATE
    for cell in s4.cells:
        if cell.cost_headroom_ratio is None:
            assert cell.meets_two_times_headroom is False
        else:
            assert cell.meets_two_times_headroom == (
                cell.cost_headroom_ratio >= S4_HEADROOM_GATE
            )
        assert cell.net_pips_positive == (cell.net_pips > 0)
        assert cell.net_r_positive == (cell.net_r > 0)
        assert cell.pips_and_r_agree_in_sign == (cell.net_pips_positive == cell.net_r_positive)


def test_zero_cost_cells_have_no_execution_cost(s4) -> None:
    for cell in s4.cells:
        if cell.configured_execution_cost_pips_per_side == 0:
            assert cell.execution_cost_pips == pytest.approx(0.0)
            assert cell.net_pips == pytest.approx(cell.gross_pips)


def test_rerun_is_byte_identical(s4) -> None:
    assert _report().model_dump_json() == s4.model_dump_json()


def test_markdown_prints_every_cell_and_explains_the_divergence(s4) -> None:
    markdown = render_s4_markdown(s4)

    assert "Where each mode stops working" in markdown
    assert "can disagree" in markdown
    assert "disagree in sign in" in markdown
    assert "modelled" in markdown
    for cell in s4.cells:
        assert (
            f"| {cell.entry_mode.value} | {cell.spread_pips_per_side:g} | "
            f"{cell.slippage_pips_per_side:g} | {cell.commission_pips_per_side:g} |"
        ) in markdown


def test_cli_writes_the_s4_artifacts(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "data" / "candles" / "XAUUSD").mkdir(parents=True)
    (tmp_path / "data" / "candles" / "XAUUSD" / "M15.jsonl").write_text(
        FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / ".env.s4test").write_text(
        "SYMBOL=XAUUSD\nTIMEFRAME=M15\nTRADING_SESSIONS=new_york\nDATA_DIR=data\nLOGS_DIR=logs\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        run(["--profile", "s4test", "--run-s4-cost-sensitivity", "--output-dir", "out"])

    assert exit_info.value.code == 0
    written = json.loads((tmp_path / "out" / "s4-cost-sensitivity.json").read_text())
    assert written["study"] == "s4_cost_sensitivity"
    assert len(written["cells"]) == S4_CELL_COUNT
    assert (tmp_path / "out" / "s4-cost-sensitivity.md").exists()
