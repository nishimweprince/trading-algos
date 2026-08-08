from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn
from pydantic import ValidationError

from api import create_app
from config import Settings, load_settings, resolve_env_file
from logging_config import configure_file_logs, configure_logging, log_event


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="cTrader Open API market-data service")
    parser.add_argument(
        "--profile",
        metavar="NAME",
        help="Load .env.NAME instead of .env",
    )
    one_shot = parser.add_mutually_exclusive_group()
    one_shot.add_argument(
        "--discover-accounts",
        action="store_true",
        help="Print every ctidTraderAccountId reachable with the access token, then exit",
    )
    one_shot.add_argument(
        "--discover-symbols",
        action="store_true",
        help="Print the broker's symbolId/symbolName/digits table, then exit",
    )
    one_shot.add_argument(
        "--refresh-token",
        action="store_true",
        help="Rotate the OAuth token pair, persist it, then exit",
    )
    return parser.parse_args(argv)


def _run_one_shot(args: argparse.Namespace, settings: Settings) -> int | None:
    """Run a one-shot action, or return None to mean "start the service".

    The flag check comes first so the normal service path never imports
    `discover` at all. The import is absolute, not relative: `sources = ["src"]`
    in pyproject.toml installs these as top-level modules, so `main` has no
    parent package for a relative import to resolve against.
    """
    if not (args.discover_accounts or args.discover_symbols or args.refresh_token):
        return None

    import discover

    configure_logging(settings.log_level)
    configure_file_logs(settings.events_log_path)
    if args.discover_accounts:
        return asyncio.run(discover.discover_accounts(settings))
    if args.discover_symbols:
        return asyncio.run(discover.discover_symbols(settings))
    return asyncio.run(discover.refresh_token(settings))


def run(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        settings = load_settings(args.profile)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    except ValidationError as exc:
        # Under launchd this is the difference between a diagnosable message and
        # a silent crash loop, so it gets the same treatment as a missing file.
        print(f"Invalid configuration in {resolve_env_file(args.profile)}:", file=sys.stderr)
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "(root)"
            print(f"  {location}: {error['msg']}", file=sys.stderr)
        sys.exit(1)

    exit_code = _run_one_shot(args, settings)
    if exit_code is not None:
        sys.exit(exit_code)

    # Logged before uvicorn binds so a port collision — the most likely launchd
    # crash-loop cause — is preceded by a line saying which port was attempted.
    configure_logging(settings.log_level)
    log_event(
        "http_server_starting",
        profile=settings.profile,
        host=settings.host,
        port=settings.port,
    )

    def app_factory() -> object:
        return create_app(settings=settings)

    uvicorn.run(
        app_factory,
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    run()
