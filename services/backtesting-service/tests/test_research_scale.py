from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from backtesting_service.cell_stats import CompletedStructure
from backtesting_service.main import parse_args, run, write_scale_sweep
from backtesting_service.models import Candle, EngineParams, EntryMode, IntrabarMode, Timeframe
from backtesting_service.research.render import render_scale_sweep_markdown
from backtesting_service.research.scale import (
    HOLD_BUCKETS,
    S8_CELL_COUNT,
    S8_ENTRY_DELAY_MINUTES,
    S8_ENTRY_MODES,
    S8_MAX_AGE_HOURS,
    S8_ORB_MINUTES,
    S8_VARIED_FIELDS,
    ScaleCoordinate,
    base_params,
    cell_params,
    hold_bucket_attribution,
    m1_coverage,
    run_scale_sweep,
    s8_grid,
)
from backtesting_service.sessions import build_windows

FIXTURE = Path(__file__).parent / "fixtures" / "xauusd_m15.jsonl"


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
            "orb_minutes": 60,
            "entry_delay_minutes": 15,
            "max_age_hours": 24,
            "time_exit_mode": "none",
        }
        | overrides
    )


def _sweep(**overrides: object):
    return run_scale_sweep(
        _candles(),
        build_windows(["tokyo", "london", "new_york"], {}),
        _params(**overrides),
        [],
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        source="local",
    )


@pytest.fixture(scope="module")
def sweep():
    return _sweep()


def test_grid_is_the_full_256_cell_cartesian_product() -> None:
    grid = s8_grid()

    assert S8_ENTRY_MODES == (
        EntryMode.HEDGE_PAIR,
        EntryMode.SYNTHETIC_BREAKOUT,
        EntryMode.CONTINGENT_HEDGE,
        EntryMode.OCO_BRACKET,
    )
    assert S8_ORB_MINUTES == (15, 30, 60, 120)
    assert S8_ENTRY_DELAY_MINUTES == (0, 15, 30, 60)
    assert S8_MAX_AGE_HOURS == (8.0, 12.0, 24.0, 48.0)
    assert S8_CELL_COUNT == 256
    assert len(grid) == 256
    assert len(set(grid)) == 256
    assert set(grid) == {
        ScaleCoordinate(mode, orb, delay, age)
        for mode in S8_ENTRY_MODES
        for orb in S8_ORB_MINUTES
        for delay in S8_ENTRY_DELAY_MINUTES
        for age in S8_MAX_AGE_HOURS
    }


def test_report_contains_every_grid_cell_exactly_once(sweep) -> None:
    assert sweep.expected_cell_count == 256
    assert len(sweep.cells) == 256
    assert [cell.cell_index for cell in sweep.cells] == list(range(256))
    coordinates = [
        (cell.entry_mode, cell.orb_minutes, cell.entry_delay_minutes, cell.max_age_hours)
        for cell in sweep.cells
    ]
    assert len(set(coordinates)) == 256
    assert set(coordinates) == {
        (
            coordinate.entry_mode,
            coordinate.orb_minutes,
            coordinate.entry_delay_minutes,
            coordinate.max_age_hours,
        )
        for coordinate in s8_grid()
    }


def test_every_cell_shares_one_fingerprint_range_and_configuration(sweep) -> None:
    candles = _candles()

    assert len(sweep.candle_set_sha256) == 64
    assert sweep.bar_count == len(candles)
    assert sweep.first_bar_ts == candles[0].ts
    assert sweep.last_bar_ts == candles[-1].ts
    for field in S8_VARIED_FIELDS:
        assert field not in sweep.shared_params
    assert sweep.shared_params["time_exit_mode"] == "max_age"
    assert sweep.shared_params["intrabar_mode"] == IntrabarMode.M1_CONSERVATIVE.value
    assert all(cell.time_exit_mode.value == "max_age" for cell in sweep.cells)


def test_cell_params_vary_only_the_four_grid_fields() -> None:
    base = base_params(_params())
    assert base.time_exit_mode.value == "max_age"

    for coordinate in s8_grid():
        cell = cell_params(base, coordinate)
        base_dump = base.model_dump()
        cell_dump = cell.model_dump()
        differing = {field for field in base_dump if base_dump[field] != cell_dump[field]}
        assert differing <= S8_VARIED_FIELDS
        assert cell.entry_mode is coordinate.entry_mode
        assert cell.orb_minutes == coordinate.orb_minutes
        assert cell.entry_delay_minutes == coordinate.entry_delay_minutes
        assert cell.max_age_hours == coordinate.max_age_hours


def test_cell_params_are_validated_not_copied_unchecked() -> None:
    base = base_params(_params(timeframe_minutes=60))
    with pytest.raises(ValueError, match="multiple of the bar timeframe"):
        cell_params(base, ScaleCoordinate(EntryMode.HEDGE_PAIR, 15, 0, 8.0))


def test_sweep_does_not_mutate_its_inputs() -> None:
    candles = _candles()
    params = _params()
    before_candles = [candle.model_dump(mode="json") for candle in candles]
    before_params = params.model_dump(mode="json")

    run_scale_sweep(
        candles,
        build_windows(["new_york"], {}),
        params,
        [],
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        source="local",
    )

    assert [candle.model_dump(mode="json") for candle in candles] == before_candles
    assert params.model_dump(mode="json") == before_params


def test_every_cell_pairs_gross_with_net(sweep) -> None:
    for cell in sweep.cells:
        assert cell.gross_pips - cell.total_cost_pips == pytest.approx(cell.net_pips)
        assert cell.gross_r >= cell.net_r
        assert cell.completed_gross_pips >= cell.completed_net_pips
        assert cell.completed_gross_r >= cell.completed_net_r
        assert cell.execution_cost_pips >= 0
        assert cell.financing_cost_pips >= 0
        assert (cell.gross_expectancy_pips is None) == (cell.net_expectancy_pips is None)
        assert (cell.gross_expectancy_r is None) == (cell.net_expectancy_r is None)
        assert (cell.gross_win_rate_excl_be is None) == (cell.net_win_rate_excl_be is None)
        assert cell.entry_fill_sides + cell.exit_fill_sides == cell.transaction_sides
        assert cell.suppressed_signals >= 0
        assert cell.unresolved_structures >= 0


def test_required_contract_fields_are_present_on_every_cell(sweep) -> None:
    required = {
        "gross_pips",
        "net_pips",
        "gross_r",
        "net_r",
        "execution_cost_pips",
        "financing_cost_pips",
        "gross_expectancy_pips",
        "net_expectancy_pips",
        "gross_expectancy_r",
        "net_expectancy_r",
        "gross_profit_factor",
        "net_profit_factor",
        "gross_win_rate_excl_be",
        "net_win_rate_excl_be",
        "gross_win_rate",
        "net_win_rate",
        "survivor_tp_rate",
        "breakeven_tp_rate_required",
        "tp_rate_margin_pp",
        "tp_rate_margin_pp_ci_low",
        "tp_rate_margin_pp_ci_high",
        "gross_max_drawdown_pips",
        "net_max_drawdown_pips",
        "gross_max_drawdown_r",
        "net_max_drawdown_r",
        "breakeven_pips_per_completed_side",
        "transaction_sides",
        "cost_side_equivalents",
        "median_hold_hours",
        "p95_hold_hours",
        "max_concurrent_structures",
        "suppressed_signals",
        "unresolved_structures",
        "prop_guard_breached",
        "prop_guard_breach_reason",
        "prop_guard_breach_events",
        "hold_buckets",
        "unbucketed_structures",
    }
    for cell in sweep.cells:
        assert required <= set(cell.model_dump().keys())


def test_hold_buckets_are_exhaustive_and_non_overlapping(sweep) -> None:
    labels = ["[0h,8h]", "(8h,12h]", "(12h,24h]", "(24h,48h]", "(48h,+inf)"]
    assert sweep.hold_bucket_labels == labels

    for cell in sweep.cells:
        assert [bucket.label for bucket in cell.hold_buckets] == labels
        bucketed = sum(bucket.structures for bucket in cell.hold_buckets)
        assert bucketed + cell.unbucketed_structures == cell.completed_structures
        assert sum(bucket.gross_r for bucket in cell.hold_buckets) == pytest.approx(
            cell.completed_gross_r
        )
        assert sum(bucket.net_r for bucket in cell.hold_buckets) == pytest.approx(
            cell.completed_net_r
        )
        assert sum(bucket.gross_pips for bucket in cell.hold_buckets) == pytest.approx(
            cell.completed_gross_pips
        )
        assert sum(bucket.net_pips for bucket in cell.hold_buckets) == pytest.approx(
            cell.completed_net_pips
        )


@pytest.mark.parametrize(
    ("hours", "label"),
    [
        (0.0, "[0h,8h]"),
        (7.99, "[0h,8h]"),
        (8.0, "[0h,8h]"),
        (8.0001, "(8h,12h]"),
        (12.0, "(8h,12h]"),
        (12.0001, "(12h,24h]"),
        (24.0, "(12h,24h]"),
        (24.0001, "(24h,48h]"),
        (48.0, "(24h,48h]"),
        (48.0001, "(48h,+inf)"),
        (10_000.0, "(48h,+inf)"),
    ],
)
def test_bucket_boundaries_are_closed_on_the_right(hours: float, label: str) -> None:
    structure = CompletedStructure(
        id="x", gross_pips=1.0, net_pips=0.5, gross_r=0.25, net_r=0.125, hold_hours=hours
    )
    buckets, unbucketed = hold_bucket_attribution([structure])

    assert unbucketed == 0
    assert [bucket.structures for bucket in buckets] == [
        1 if bucket.label == label else 0 for bucket in buckets
    ]
    assert sum(bucket.gross_r for bucket in buckets) == pytest.approx(0.25)
    assert sum(bucket.net_r for bucket in buckets) == pytest.approx(0.125)


def test_structures_without_an_exit_are_counted_as_unbucketed_not_dropped() -> None:
    buckets, unbucketed = hold_bucket_attribution(
        [
            CompletedStructure("a", 1.0, 0.5, 0.25, 0.1, 4.0),
            CompletedStructure("b", 2.0, 1.5, 0.50, 0.2, None),
        ]
    )

    assert unbucketed == 1
    assert sum(bucket.structures for bucket in buckets) == 1
    assert len(HOLD_BUCKETS) == 5


def test_m1_coverage_is_reported_and_names_the_no_subpath_fallback(sweep) -> None:
    coverage = sweep.m1_coverage

    assert coverage.intrabar_mode is IntrabarMode.M1_CONSERVATIVE
    assert coverage.status == "absent"
    assert coverage.m1_bars_loaded == 0
    assert coverage.covered_parent_bars == 0
    assert coverage.covered_parent_fraction == 0.0
    assert coverage.subpath_used is False
    assert coverage.subpath_fallback == "pessimistic_same_bar_no_subpath"
    assert "no M1 chronology" in coverage.fallback_description


def test_m1_coverage_reports_complete_when_every_parent_bar_is_covered() -> None:
    candles = _candles()[:2]
    m1_bars = [
        candle.model_copy(update={"ts": candle.ts - timedelta(minutes=offset)})
        for candle in candles
        for offset in range(3)
    ]

    coverage = m1_coverage(candles, m1_bars, _params())

    assert coverage.status == "complete"
    assert coverage.covered_parent_bars == len(candles)
    assert coverage.subpath_used is True
    assert coverage.subpath_fallback is None


def test_rerun_on_the_same_inputs_is_byte_identical(sweep) -> None:
    again = _sweep()

    assert again.model_dump_json() == sweep.model_dump_json()
    assert render_scale_sweep_markdown(again) == render_scale_sweep_markdown(sweep)


def test_markdown_prints_every_cell_and_refuses_to_pick_a_winner(sweep) -> None:
    markdown = render_scale_sweep_markdown(sweep)

    assert sweep.candle_set_sha256 in markdown
    assert "pessimistic_same_bar_no_subpath" in markdown
    assert "no cell is recommended for production" in markdown
    for label in sweep.hold_bucket_labels:
        assert label in markdown
    for cell in sweep.cells:
        assert (
            f"| {cell.cell_index} | {cell.entry_mode.value} | {cell.orb_minutes} | "
            f"{cell.entry_delay_minutes} | {cell.max_age_hours:.0f} |"
        ) in markdown


def test_cli_exposes_the_s8_command_as_a_one_shot() -> None:
    args = parse_args(["--run-s8-scale-sweep", "--symbol", "XAUUSD", "--output-dir", "out"])

    assert args.run_s8_scale_sweep is True
    assert args.compare_entry_modes is False
    assert str(args.output_dir) == "out"
    with pytest.raises(SystemExit):
        parse_args(["--run-s8-scale-sweep", "--compare-entry-modes"])


def test_cli_writes_both_artifacts_end_to_end(tmp_path: Path, monkeypatch) -> None:
    workdir = tmp_path / "work"
    (workdir / "data" / "candles" / "XAUUSD").mkdir(parents=True)
    (workdir / "data" / "candles" / "XAUUSD" / "M15.jsonl").write_text(
        FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (workdir / ".env.s8test").write_text(
        "SYMBOL=XAUUSD\nTIMEFRAME=M15\nTRADING_SESSIONS=new_york\nDATA_DIR=data\nLOGS_DIR=logs\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(workdir)

    with pytest.raises(SystemExit) as exit_info:
        run(["--profile", "s8test", "--run-s8-scale-sweep", "--output-dir", "reports/research"])

    assert exit_info.value.code == 0
    written = json.loads(
        (workdir / "reports" / "research" / "s8-scale-decomposition.json").read_text()
    )
    markdown = (workdir / "reports" / "research" / "s8-scale-decomposition.md").read_text()
    assert written["study"] == "s8_scale_decomposition"
    assert len(written["cells"]) == 256
    assert written["m1_coverage"]["status"] == "absent"
    assert written["candle_set_sha256"] in markdown


def test_cli_refuses_a_non_m15_timeframe(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env.s8test").write_text(
        "SYMBOL=XAUUSD\nTIMEFRAME=M15\nTRADING_SESSIONS=new_york\nDATA_DIR=data\nLOGS_DIR=logs\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        run(["--profile", "s8test", "--run-s8-scale-sweep", "--timeframe", "H1"])

    assert exit_info.value.code == 1


def test_write_scale_sweep_round_trips_the_report(tmp_path: Path, sweep) -> None:
    json_path, markdown_path = write_scale_sweep(sweep, tmp_path / "nested" / "research")

    assert json_path.name == "s8-scale-decomposition.json"
    assert markdown_path.name == "s8-scale-decomposition.md"
    assert json.loads(json_path.read_text())["expected_cell_count"] == 256
    assert markdown_path.read_text() == render_scale_sweep_markdown(sweep)


def test_markdown_states_the_entry_delay_degeneracy(sweep) -> None:
    markdown = render_scale_sweep_markdown(sweep)

    assert "Structural degeneracy on the entry-delay axis" in markdown
    assert "duplicates by construction" in markdown
    # ENTRY_DELAY at or below ORB is absorbed by the opening-range close, so the
    # smallest ORB (15) makes the 0 and 15 minute delays the same configuration.
    collapsed = {
        (cell.entry_mode, cell.orb_minutes, cell.max_age_hours): cell
        for cell in sweep.cells
        if cell.entry_delay_minutes == 0
    }
    for cell in sweep.cells:
        if cell.entry_delay_minutes != 15:
            continue
        twin = collapsed[(cell.entry_mode, cell.orb_minutes, cell.max_age_hours)]
        assert cell.gross_r == twin.gross_r
        assert cell.net_r == twin.net_r
        assert cell.completed_structures == twin.completed_structures


def test_partial_m1_coverage_falls_back_uniformly_across_the_window() -> None:
    candles = _candles()
    # Cover only the first two parent bars: a mixed-tier run is exactly what this refuses.
    m1_bars = [
        candles[index].model_copy(update={"ts": candles[index].ts - timedelta(minutes=offset)})
        for index in (0, 1)
        for offset in range(3)
    ]

    coverage = m1_coverage(candles, m1_bars, _params())

    assert coverage.status == "partial"
    assert coverage.m1_bars_loaded == 6
    assert coverage.covered_parent_bars == 2
    assert coverage.subpath_used is False
    assert coverage.subpath_fallback == "pessimistic_same_bar_no_subpath"
    assert "no M1 chronology was used" in coverage.fallback_description
    assert "covered only 2 of" in coverage.fallback_description


def test_partial_coverage_does_not_feed_m1_bars_to_the_engine() -> None:
    candles = _candles()
    m1_bars = [candles[0].model_copy(update={"ts": candles[0].ts - timedelta(minutes=1)})]

    with_partial = run_scale_sweep(
        candles,
        build_windows(["new_york"], {}),
        _params(),
        [],
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        source="local",
        m1_bars=m1_bars,
    )
    without = run_scale_sweep(
        candles,
        build_windows(["new_york"], {}),
        _params(),
        [],
        symbol="XAUUSD",
        timeframe=Timeframe.M15,
        source="local",
    )

    assert with_partial.m1_coverage.status == "partial"
    assert without.m1_coverage.status == "absent"
    assert [cell.net_r for cell in with_partial.cells] == [cell.net_r for cell in without.cells]
