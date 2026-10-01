from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from ta_core import load_or_exit, serve

from .api import create_app
from .config import Settings, load_settings
from .logging_config import LOGGER_NAME, configure_file_logs, configure_logging


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Broker-agnostic market-data and trade-execution gateway"
    )
    parser.add_argument(
        "--profile",
        metavar="NAME",
        help="Load .env.NAME instead of .env",
    )
    parser.add_argument(
        "--account",
        metavar="ALIAS",
        help="Account registry alias used by account-scoped discovery commands",
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
    one_shot.add_argument(
        "--migrate-legacy-ledger",
        nargs="?",
        const=True,
        metavar="SIGNALS_DB",
        help=(
            "Import the pre-unification MT5 signals.db (default: DATABASE_PATH) into "
            "EXECUTION_DATABASE_PATH, then exit. Startup also does this once"
        ),
    )
    one_shot.add_argument(
        "--validate-config",
        action="store_true",
        help="Validate the environment and account registry without connecting, then exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --migrate-legacy-ledger: report what would be imported, write nothing",
    )
    return parser.parse_args(argv)


def _migrate(args: argparse.Namespace, settings: Settings) -> int:
    from ta_store import ExecutionRepository

    from .migration import migrate_legacy_ledger

    if "mt5" not in settings.adapters:
        print("The legacy signal ledger exists only on MT5 hosts (ADAPTERS=mt5).", file=sys.stderr)
        return 1
    source = (
        settings.database_path
        if args.migrate_legacy_ledger is True
        else Path(args.migrate_legacy_ledger)
    )
    repository = ExecutionRepository(settings.execution_database_path)
    repository.initialize()
    summary = migrate_legacy_ledger(
        source, repository, settings.profile or "mt5", dry_run=args.dry_run
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if summary.get("missing") else 0


def _run_one_shot(args: argparse.Namespace, settings: Settings) -> int | None:
    """Run a one-shot action, or return None to mean "start the service".

    The flag check comes first so the normal service path never imports
    `discover` at all. The import is absolute, not relative: `sources = ["src"]`
    in pyproject.toml installs these as top-level modules, so `main` has no
    parent package for a relative import to resolve against.
    """
    if args.validate_config:
        if settings.gateway_enabled:
            environments = sorted({account.environment for account in settings.gateway_accounts})
            print(
                f"Valid gateway configuration: {len(settings.gateway_accounts)} runtime "
                f"account candidates across {','.join(environments)}"
            )
        else:
            print("Valid legacy single-account configuration.")
        return 0
    if args.migrate_legacy_ledger is not None:
        return _migrate(args, settings)
    if not (args.discover_accounts or args.discover_symbols or args.refresh_token):
        return None

    from ta_plugin_ctrader import discover

    configure_logging(settings.log_level)
    configure_file_logs(settings.events_log_path)
    if args.discover_accounts:
        return asyncio.run(discover.discover_accounts(settings))
    if args.discover_symbols:
        discovery_settings = settings
        if settings.gateway_enabled:
            alias = args.account or settings.default_market_data_account
            assert alias is not None
            try:
                account = settings.account(alias)
            except KeyError:
                print(f"Unknown or disabled account alias: {alias}", file=sys.stderr)
                return 1
            discovery_settings = settings.model_copy(
                update={
                    "account_id": account.ctid_trader_account_id,
                    "environment": account.environment,
                }
            )
        return asyncio.run(discover.discover_symbols(discovery_settings))
    return asyncio.run(discover.refresh_token(settings))


def run(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    settings = load_or_exit(load_settings, args.profile)

    exit_code = _run_one_shot(args, settings)
    if exit_code is not None:
        sys.exit(exit_code)

    def app_factory() -> object:
        return create_app(settings=settings)

    serve(settings, app_factory, logger_name=LOGGER_NAME)


if __name__ == "__main__":
    run()
