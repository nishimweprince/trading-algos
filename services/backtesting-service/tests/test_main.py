from __future__ import annotations

from backtesting_service.main import _seed_count, _seed_timeframe, parse_args
from backtesting_service.models import Timeframe
from backtesting_service.research.cli import COMMANDS


def test_seed_m1_flag_selects_m1() -> None:
    args = parse_args(["--seed-m1"])
    assert args.seed_m1 is True
    assert args.seed is False
    assert _seed_timeframe(args, type("S", (), {"timeframe": Timeframe.M15})()) is Timeframe.M1
    assert _seed_count(args, Timeframe.M1) == 20_000


def test_seed_m1_respects_explicit_count() -> None:
    args = parse_args(["--seed-m1", "--count", "500"])
    assert _seed_count(args, Timeframe.M1) == 500


def test_seed_timeframe_m1_is_a_listed_choice() -> None:
    args = parse_args(["--seed", "--timeframe", "M1"])
    assert args.timeframe == "M1"
    settings = type("S", (), {"timeframe": Timeframe.M15})()
    assert _seed_timeframe(args, settings) is Timeframe.M1


def test_compare_entry_modes_is_a_one_shot_command() -> None:
    args = parse_args(
        [
            "--compare-entry-modes",
            "--symbol",
            "XAUUSD",
            "--timeframe",
            "H1",
            "--date-from",
            "2026-01-01T00:00:00+00:00",
        ]
    )
    assert args.compare_entry_modes is True
    assert args.timeframe == "H1"
    assert args.date_from.utcoffset() is not None


def test_phase3_exploratory_is_a_one_shot_command() -> None:
    args = parse_args(["--run-phase3-exploratory"])
    assert args.run_phase3_exploratory is True
    assert args.run_phase3_holdout is False


def test_phase3_holdout_is_a_one_shot_command() -> None:
    args = parse_args(["--run-phase3-holdout"])
    assert args.run_phase3_holdout is True


def test_hedge_survivor_development_is_a_one_shot_command() -> None:
    args = parse_args(["--run-hedge-survivor-development"])
    assert args.run_hedge_survivor_development is True


def test_every_research_flag_has_a_driver() -> None:
    """The dispatch table and the parser cannot drift apart.

    run() used to be a chain of near-identical
    `if args.x: sys.exit(_run_x(settings, args))` blocks. A table is shorter,
    but it makes a new --run-* flag that nobody wired up fail silently: the
    parser accepts it and the service starts the HTTP server instead. This is
    what stops that.
    """
    parsed = parse_args([])
    flags = {name for name in vars(parsed) if name.startswith("run_")}
    assert flags == {flag for flag, _ in COMMANDS}


def test_no_research_flag_is_set_by_default() -> None:
    """Bare `backtesting-service` serves HTTP; it must not fall into a study."""
    parsed = parse_args([])
    assert not any(getattr(parsed, flag) for flag, _ in COMMANDS)
