"""``zt`` command line. Subcommands are added milestone by milestone (see PLAN.md)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from zone_trend.config import DEFAULT_PATH, load_config


def _check_config(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    print(f"ok: {args.config} (mode={cfg.mode.value})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zt", description="Zone-trend strategy tooling")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("config", help="validate a config file")
    check.add_argument("--config", type=Path, default=DEFAULT_PATH)
    check.set_defaults(func=_check_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
