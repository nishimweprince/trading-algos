from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backtesting_service.anchors import build_anchors
from backtesting_service.main import run
from backtesting_service.models import Candle, EngineParams, Timeframe
from backtesting_service.research.s9_regime import (
    S9_CONCENTRATION_THRESHOLD,
    S9_MODES,
    render_s9_markdown,
    run_s9_regime_attribution,
    trend_regimes,
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


def _params() -> EngineParams:
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
    )


def _report():
    return run_s9_regime_attribution(
        _candles(),
        build_windows(SESSIONS, {}),
        _params(),
        build_anchors(SESSIONS),
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        source="local",
    )


@pytest.fixture(scope="module")
def s9():
    return _report()


def _daily(closes: list[float]) -> list[Candle]:
    return [
        Candle(
            ts=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=1.0,
            provider="fixture",
            source_instrument="XAUUSD",
        )
        for index, close in enumerate(closes)
    ]


def test_trend_regimes_label_by_trailing_slope_with_a_deadband() -> None:
    rising = [100.0 + index * 10 for index in range(12)]
    labels = trend_regimes(
        _daily(rising), lookback_days=5, deadband_pips_per_day=50.0, pip_size=0.1
    )

    days = sorted(labels)
    assert [labels[day] for day in days[:5]] == ["warmup"] * 5
    # 10 price units a day is 100 pips a day, well beyond the 50-pip deadband.
    assert all(labels[day] == "up" for day in days[5:])

    flat = [100.0 + index * 0.1 for index in range(12)]
    flat_labels = trend_regimes(
        _daily(flat), lookback_days=5, deadband_pips_per_day=50.0, pip_size=0.1
    )
    assert all(flat_labels[day] in {"warmup", "flat"} for day in flat_labels)

    falling = [200.0 - index * 10 for index in range(12)]
    fall_labels = trend_regimes(
        _daily(falling), lookback_days=5, deadband_pips_per_day=50.0, pip_size=0.1
    )
    assert any(label == "down" for label in fall_labels.values())


def test_every_mode_is_split_the_same_way(s9) -> None:
    assert s9.entry_modes == list(S9_MODES)
    for mode in S9_MODES:
        splits = {(cell.split_kind, cell.split_key) for cell in s9.cells if cell.entry_mode is mode}
        assert ("all", "all") in splits
        assert {("calendar_half", "first"), ("calendar_half", "second")} <= splits
        assert {("trend_regime", regime) for regime in ("up", "down", "flat", "warmup")} <= splits


def test_splits_partition_the_completed_structures(s9) -> None:
    for mode in S9_MODES:
        cells = [cell for cell in s9.cells if cell.entry_mode is mode]
        overall = next(cell for cell in cells if cell.split_kind == "all")
        for kind in ("calendar_half", "trend_regime", "session"):
            subset = [cell for cell in cells if cell.split_kind == kind]
            assert sum(cell.completed_structures for cell in subset) == (
                overall.completed_structures
            )
            assert sum(cell.net_r for cell in subset) == pytest.approx(overall.net_r)
            assert sum(cell.gross_pips for cell in subset) == pytest.approx(overall.gross_pips)


def test_winner_split_counts_are_consistent(s9) -> None:
    for cell in s9.cells:
        assert cell.long_winners + cell.short_winners <= cell.tp_structures
        assert cell.tp_structures <= cell.completed_structures
        if cell.long_winner_share is not None:
            assert cell.long_winner_ci_low <= cell.long_winner_share <= cell.long_winner_ci_high
        assert cell.gross_r >= cell.net_r


def test_flags_only_fire_above_the_threshold(s9) -> None:
    assert s9.concentration_threshold == S9_CONCENTRATION_THRESHOLD
    for flag in s9.flags:
        assert flag.reason in {
            "directional_winner_concentration",
            "calendar_half_concentration",
            "trend_regime_concentration",
        }
        assert flag.detail
    for mode in S9_MODES:
        overall = next(
            cell for cell in s9.cells if cell.entry_mode is mode and cell.split_kind == "all"
        )
        directional = [
            flag
            for flag in s9.flags
            if flag.entry_mode is mode and flag.reason == "directional_winner_concentration"
        ]
        if overall.long_winner_share is None:
            assert not directional
        else:
            extreme = (
                overall.long_winner_share >= S9_CONCENTRATION_THRESHOLD
                or overall.long_winner_share <= 1 - S9_CONCENTRATION_THRESHOLD
            )
            assert bool(directional) == (extreme and overall.tp_structures > 0)


def test_rerun_is_byte_identical(s9) -> None:
    assert _report().model_dump_json() == s9.model_dump_json()


def test_markdown_states_the_window_trend_and_every_split(s9) -> None:
    markdown = render_s9_markdown(s9)

    assert "A symmetric straddle in a trending instrument collects the drift." in markdown
    assert "Directional and regime flags" in markdown
    assert f"{s9.price_change_pips:+.1f} pips" in markdown
    for cell in s9.cells:
        assert f"| {cell.entry_mode.value} | {cell.split_kind} | {cell.split_key} |" in markdown


def test_cli_writes_the_s9_artifacts(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "data" / "candles" / "XAUUSD").mkdir(parents=True)
    (tmp_path / "data" / "candles" / "XAUUSD" / "M15.jsonl").write_text(
        FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / ".env.s9test").write_text(
        "SYMBOL=XAUUSD\nTIMEFRAME=M15\nTRADING_SESSIONS=new_york\nDATA_DIR=data\nLOGS_DIR=logs\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        run(["--profile", "s9test", "--run-s9-regime-attribution", "--output-dir", "out"])

    assert exit_info.value.code == 0
    written = json.loads((tmp_path / "out" / "s9-regime-attribution.json").read_text())
    assert written["study"] == "s9_regime_attribution"
    assert written["trend_lookback_days"] == 5
    assert (tmp_path / "out" / "s9-regime-attribution.md").exists()
