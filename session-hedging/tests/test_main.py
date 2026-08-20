from __future__ import annotations

from main import _seed_count, _seed_timeframe, parse_args
from models import Timeframe


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
