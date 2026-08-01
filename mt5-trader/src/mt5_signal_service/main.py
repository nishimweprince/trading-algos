from __future__ import annotations

import argparse
import sys

import uvicorn

from .api import create_app
from .config import load_settings


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MT5 signal execution service")
    parser.add_argument(
        "--profile",
        metavar="NAME",
        help="Load .env.NAME instead of .env",
    )
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        settings = load_settings(args.profile)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)

    def app_factory() -> object:
        return create_app(settings=settings)

    uvicorn.run(
        app_factory,
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=1,
    )


if __name__ == "__main__":
    run()
