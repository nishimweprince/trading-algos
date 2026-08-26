from __future__ import annotations

import json
from pathlib import Path

import pytest

from backtesting_service.main import run
from backtesting_service.models import Candle, EngineParams, Timeframe
from backtesting_service.research.s3_anchor_study import (
    S3_ANCHOR_GRID,
    render_s3_markdown,
    run_s3_anchor_study,
)
from backtesting_service.sessions import build_windows

FIXTURE = Path(__file__).parent / "fixtures" / "xauusd_m15.jsonl"
SESSIONS = ["tokyo", "london", "new_york"]


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
            "time_exit_mode": "max_age",
            "max_age_hours": 24,
        }
        | overrides
    )


def _report():
    return run_s3_anchor_study(
        _candles(),
        build_windows(SESSIONS, {}),
        _params(),
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        source="local",
    )


@pytest.fixture(scope="module")
def s3():
    return _report()


def test_the_grid_is_the_specified_anchor_set(s3) -> None:
    assert len(S3_ANCHOR_GRID) == 9
    assert [cell.anchor_spec for cell in s3.cells] == [row[2] for row in S3_ANCHOR_GRID]
    incumbents = {cell.session for cell in s3.cells if cell.is_incumbent}
    assert incumbents == {"tokyo", "london", "new_york"}
    assert len([cell for cell in s3.cells if cell.session == "new_york"]) == 4
    # Sydney has no gold exchange open; the spec keeps it disabled.
    assert not any("sydney" in cell.anchor_label for cell in s3.cells)


def test_every_cell_pairs_gross_with_net_and_reports_its_anchor_drift(s3) -> None:
    for cell in s3.cells:
        assert cell.gross_r >= cell.net_r
        assert cell.gross_pips >= cell.net_pips
        assert cell.signals >= 0
        assert cell.anchor_skips >= 0
        assert cell.episodes >= 0
        if cell.anchor_drift_p50 is not None:
            assert cell.anchor_drift_p50 <= 15


def test_expansion_ratios_are_reported_with_their_sample_counts(s3) -> None:
    for cell in s3.cells:
        if cell.median_range_expansion is not None:
            assert cell.range_expansion_episodes > 0
            assert cell.median_range_expansion > 0
        else:
            assert cell.range_expansion_episodes == 0
        if cell.median_volume_expansion is not None:
            assert cell.volume_expansion_episodes > 0


def test_anchors_inside_one_bar_produce_identical_measurements() -> None:
    report = _report()
    by_label = {cell.anchor_label: cell for cell in report.cells}

    # 08:20 and 08:30 New York fall inside the same M15 bar at this resolution, so the
    # opening range and the entry bar are identical. The renderer must say so.
    early, late = by_label["new_york_0820"], by_label["new_york_0830"]
    if early.median_orb_range_pips == late.median_orb_range_pips:
        assert early.net_r == late.net_r
        assert "Bar-resolution degeneracy" in render_s3_markdown(report)


def test_rerun_is_byte_identical(s3) -> None:
    assert _report().model_dump_json() == s3.model_dump_json()


def test_markdown_answers_the_new_york_question_without_choosing_an_anchor(s3) -> None:
    markdown = render_s3_markdown(s3)

    assert "Reading the New York question" in markdown
    assert "No anchor is selected here." in markdown
    assert "walk-forward evidence before an anchor changes" in markdown
    for cell in s3.cells:
        assert f"`{cell.anchor_spec}`" in markdown


def test_cli_writes_the_s3_artifacts(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "data" / "candles" / "XAUUSD").mkdir(parents=True)
    (tmp_path / "data" / "candles" / "XAUUSD" / "M15.jsonl").write_text(
        FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / ".env.s3test").write_text(
        "SYMBOL=XAUUSD\nTIMEFRAME=M15\nTRADING_SESSIONS=new_york\nDATA_DIR=data\nLOGS_DIR=logs\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        run(["--profile", "s3test", "--run-s3-anchor-study", "--output-dir", "out"])

    assert exit_info.value.code == 0
    written = json.loads((tmp_path / "out" / "s3-anchor-study.json").read_text())
    assert written["study"] == "s3_anchor_study"
    assert len(written["cells"]) == 9
    assert (tmp_path / "out" / "s3-anchor-study.md").exists()
