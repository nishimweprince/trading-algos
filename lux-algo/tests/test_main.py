from __future__ import annotations

from lux_algo.main import parse_args


def test_parse_args_default() -> None:
    args = parse_args([])
    assert args.profile is None


def test_parse_args_profile() -> None:
    args = parse_args(["--profile", "deriv"])
    assert args.profile == "deriv"
