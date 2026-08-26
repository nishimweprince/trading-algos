"""The argparse + settings + uvicorn bootstrap shared by every service entrypoint.

The error handling here is the point of the module. Under launchd a
configuration mistake is otherwise a silent crash loop, so a missing env file,
a failed field validation and a failed cross-field validation each print a
diagnosable message and exit 1.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from typing import Any

import uvicorn
from pydantic import ValidationError

from .logging_config import configure_logging, log_event
from .settings import BaseServiceSettings, resolve_env_file


def base_parser(description: str) -> argparse.ArgumentParser:
    """A parser carrying `--profile`. Services add their own one-shot flags."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--profile", metavar="NAME", help="Load .env.NAME instead of .env")
    return parser


def load_or_exit(
    loader: Callable[[str | None], BaseServiceSettings],
    profile: str | None,
) -> Any:
    try:
        return loader(profile)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    except ValidationError as exc:
        print(f"Invalid configuration in {resolve_env_file(profile)}:", file=sys.stderr)
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "(root)"
            print(f"  {location}: {error['msg']}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"Invalid configuration in {resolve_env_file(profile)}:", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        sys.exit(1)


def serve(
    settings: BaseServiceSettings,
    app_factory: Callable[[], Any],
    *,
    logger_name: str,
) -> None:
    """Configure logging, announce the bind, then hand off to uvicorn.

    The announcement precedes the bind so a port collision — the most likely
    launchd crash-loop cause — is preceded by a line saying which port was
    attempted.
    """
    configure_logging(settings.log_level, name=logger_name)
    log_event(
        "http_server_starting",
        profile=settings.profile,
        host=settings.host,
        port=settings.port,
    )
    uvicorn.run(
        app_factory,
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=1,
        access_log=False,
    )
