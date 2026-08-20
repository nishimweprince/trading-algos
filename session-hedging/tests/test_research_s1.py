from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from anchors import build_anchors
from main import parse_args, run
from models import Candle, EngineParams, Timeframe
from research.s1_target_hit import (
    S1_HORIZON_HOURS,
    S1_K_VALUES,
    lock_price_for,
    render_s1_markdown,
    run_s1_target_hit,
    survivor_excursions,
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


def _bar(minute: int, high: float, low: float) -> Candle:
    return Candle(
        ts=datetime(2026, 1, 14, 0, 0, tzinfo=UTC) + timedelta(minutes=minute),
        open=(high + low) / 2,
        high=high,
        low=low,
        close=(high + low) / 2,
        volume=1.0,
        provider="fixture",
        source_instrument="XAUUSD",
    )


def _report(**overrides: object):
    return run_s1_target_hit(
        _candles(),
        build_windows(SESSIONS, {}),
        _params(**overrides),
        build_anchors(SESSIONS),
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        source="local",
    )


@pytest.fixture(scope="module")
def s1():
    return _report()


def test_forward_walk_excludes_the_stop_bar_and_scales_in_r() -> None:
    stop_bar = _bar(15, high=2000.0, low=1000.0)
    candles = [
        stop_bar,
        _bar(30, high=110.0, low=95.0),
        _bar(45, high=130.0, low=90.0),
    ]

    walk = survivor_excursions(
        candles,
        first_stop_ts=stop_bar.ts,
        entry=100.0,
        sl_dist=10.0,
        lock_price=80.0,
        is_long=True,
    )

    assert walk is not None
    assert walk.forward_bars == 2
    # The stop bar's 2000 high is never credited: 130 is 3R above a 100 entry with S=10.
    assert walk.mfe_r["48h"] == pytest.approx(3.0)
    assert walk.mae_r["48h"] == pytest.approx(1.0)
    assert walk.lock_touched_ts is None


def test_forward_walk_stops_crediting_after_the_lock_is_touched() -> None:
    stop_bar = _bar(15, high=101.0, low=99.0)
    candles = [
        stop_bar,
        _bar(30, high=115.0, low=105.0),
        _bar(45, high=108.0, low=80.0),
        _bar(60, high=200.0, low=150.0),
    ]

    walk = survivor_excursions(
        candles,
        first_stop_ts=stop_bar.ts,
        entry=100.0,
        sl_dist=10.0,
        lock_price=102.0,
        is_long=True,
    )

    assert walk is not None
    assert walk.lock_touched_ts == candles[2].ts
    assert walk.mfe_r["48h"] == pytest.approx(10.0)
    # Only the pre-lock bar counts once the lock is touched, and the touching bar itself
    # is not credited: 115 is 1.5R, the later 200 is not reachable after a lock exit.
    assert walk.mfe_r_before_lock["48h"] == pytest.approx(1.5)


def test_horizons_censor_the_walk() -> None:
    stop_bar = _bar(0, high=100.0, low=100.0)
    candles = [stop_bar] + [
        _bar(60 * hours, high=100.0 + hours, low=100.0) for hours in range(1, 30)
    ]

    walk = survivor_excursions(
        candles,
        first_stop_ts=stop_bar.ts,
        entry=100.0,
        sl_dist=1.0,
        lock_price=90.0,
        is_long=True,
    )

    assert walk is not None
    assert walk.mfe_r["1h"] == pytest.approx(1.0)
    assert walk.mfe_r["4h"] == pytest.approx(4.0)
    assert walk.mfe_r["24h"] == pytest.approx(24.0)
    assert walk.mfe_r["48h"] == pytest.approx(29.0)


def test_lock_price_mirrors_the_engine_rule() -> None:
    assert lock_price_for(100.0, sl_dist=10.0, lock_dist=2.0, is_long=True) == 102.0
    assert lock_price_for(100.0, sl_dist=10.0, lock_dist=2.0, is_long=False) == 98.0
    # S below the lock distance collapses the lock to entry, exactly as _apply_lock does.
    assert lock_price_for(100.0, sl_dist=1.0, lock_dist=2.0, is_long=True) == 100.0
    assert lock_price_for(100.0, sl_dist=10.0, lock_dist=0.0, is_long=True) == 100.0


def test_conditioning_counts_account_for_every_structure(s1) -> None:
    summary = s1.conditioning

    assert summary.structures_total == (
        summary.conditioned
        + summary.excluded_no_stop
        + summary.excluded_simultaneous_stop
        + summary.excluded_not_two_legs
        + summary.excluded_missing_entry
        + summary.excluded_no_forward_bars
    )
    assert summary.conditioned == len(s1.structures)
    assert summary.lock_touched == sum(
        1 for item in s1.structures if item.lock_touched_ts is not None
    )


def test_every_group_horizon_and_k_combination_is_reported(s1) -> None:
    assert s1.k_values == list(S1_K_VALUES)
    assert s1.horizon_hours == list(S1_HORIZON_HOURS)
    groups = {(cell.group_kind, cell.group_key) for cell in s1.reach_cells}
    assert ("all", "all") in groups
    for group in groups:
        cells = [
            (cell.horizon_hours, cell.k)
            for cell in s1.reach_cells
            if (cell.group_kind, cell.group_key) == group
        ]
        assert sorted(cells) == sorted(
            (horizon, k) for horizon in S1_HORIZON_HOURS for k in S1_K_VALUES
        )
    assert len(s1.excursions) == len(groups) * len(S1_HORIZON_HOURS)


def test_reach_rates_are_monotone_and_bracketed_by_their_intervals(s1) -> None:
    for cell in s1.reach_cells:
        for rate in (cell.unconditional, cell.lock_survived):
            assert 0 <= rate.reached <= rate.n
            if rate.rate is not None:
                assert rate.ci_low is not None and rate.ci_high is not None
                assert rate.ci_low <= rate.rate <= rate.ci_high
        # A lock can only remove reaches, never add them.
        assert cell.lock_survived.reached <= cell.unconditional.reached

    by_key = {
        (cell.group_kind, cell.group_key, cell.horizon_hours, cell.k): cell
        for cell in s1.reach_cells
    }
    for (kind, key, horizon, k), cell in by_key.items():
        larger_k = [value for value in S1_K_VALUES if value > k]
        if larger_k:
            nxt = by_key[(kind, key, horizon, min(larger_k))]
            assert nxt.unconditional.reached <= cell.unconditional.reached
        longer = [value for value in S1_HORIZON_HOURS if value > horizon]
        if longer:
            nxt = by_key[(kind, key, min(longer), k)]
            assert nxt.unconditional.reached >= cell.unconditional.reached


def test_excursions_are_non_negative_and_ordered(s1) -> None:
    for cell in s1.excursions:
        if cell.n == 0:
            continue
        assert cell.mfe_pips_median is not None and cell.mfe_pips_median >= 0
        assert cell.mae_pips_median is not None and cell.mae_pips_median >= 0
        assert cell.mfe_pips_p95 >= cell.mfe_pips_median
        assert cell.mae_pips_p95 >= cell.mae_pips_median


def test_structures_carry_every_horizon_and_a_conservative_lock_series(s1) -> None:
    keys = {f"{horizon:g}h" for horizon in S1_HORIZON_HOURS}
    for item in s1.structures:
        assert set(item.mfe_r_by_horizon) == keys
        assert set(item.mae_r_by_horizon) == keys
        assert set(item.mfe_r_before_lock_by_horizon) == keys
        for key in keys:
            assert item.mfe_r_before_lock_by_horizon[key] <= item.mfe_r_by_horizon[key]
        assert item.s_pips > 0
        assert item.atr_tercile in {"low", "mid", "high", "unclassified"}


def test_rerun_is_byte_identical(s1) -> None:
    assert _report().model_dump_json() == s1.model_dump_json()


def test_markdown_reports_conditioning_caveats_and_every_cell(s1) -> None:
    markdown = render_s1_markdown(s1)

    assert s1.candle_set_sha256 in markdown
    assert "upper bounds" in markdown
    assert "selects no `RR`" in markdown
    assert s1.m1_coverage.fallback_description in markdown
    for cell in s1.reach_cells:
        assert (
            f"| {cell.group_kind} | {cell.group_key} | {cell.horizon_hours:g} | {cell.k:g} |"
            in markdown
        )


def test_cli_writes_the_s1_artifacts(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "data" / "candles" / "XAUUSD").mkdir(parents=True)
    (tmp_path / "data" / "candles" / "XAUUSD" / "M15.jsonl").write_text(
        FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / ".env.s1test").write_text(
        "SYMBOL=XAUUSD\nTIMEFRAME=M15\nTRADING_SESSIONS=new_york\nDATA_DIR=data\nLOGS_DIR=logs\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert parse_args(["--run-s1-target-hit"]).run_s1_target_hit is True
    with pytest.raises(SystemExit) as exit_info:
        run(["--profile", "s1test", "--run-s1-target-hit", "--output-dir", "reports/research"])

    assert exit_info.value.code == 0
    written = json.loads(
        (tmp_path / "reports" / "research" / "s1-conditional-target-hit.json").read_text()
    )
    assert written["study"] == "s1_conditional_target_hit"
    assert written["m1_coverage"]["status"] == "absent"
    assert (tmp_path / "reports" / "research" / "s1-conditional-target-hit.md").exists()
