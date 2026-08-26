"""Entry-point tests.

`main` had no test at all, and shipped with a relative import (`from . import
discover`) in a module that pyproject installs at top level. Every invocation —
including the plain service start, because the one-shot dispatch runs before any
flag is inspected — died with ImportError while CI stayed green. These tests
exist so the console script cannot break again without failing the build.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from execution_service import main
from execution_service.config import Settings
from tests.conftest import ENV_TEMPLATE

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _write_env(tmp_path: Path, profile: str = "forex") -> Path:
    env_file = tmp_path / f".env.{profile}"
    env_file.write_text(ENV_TEMPLATE, encoding="utf-8")
    return env_file


# --- argument parsing --------------------------------------------------------


def test_parse_args_defaults_to_no_profile_and_no_one_shot() -> None:
    args = main.parse_args([])

    assert args.profile is None
    assert not args.discover_accounts
    assert not args.discover_symbols
    assert not args.refresh_token
    assert not args.validate_config


@pytest.mark.parametrize(
    ("flag", "attribute"),
    [
        ("--discover-accounts", "discover_accounts"),
        ("--discover-symbols", "discover_symbols"),
        ("--refresh-token", "refresh_token"),
        ("--validate-config", "validate_config"),
    ],
)
def test_parse_args_accepts_each_one_shot_flag(flag: str, attribute: str) -> None:
    args = main.parse_args(["--profile", "forex", flag])

    assert args.profile == "forex"
    assert getattr(args, attribute) is True


def test_one_shot_flags_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        main.parse_args(["--discover-accounts", "--discover-symbols"])


# --- one-shot dispatch -------------------------------------------------------


def test_run_one_shot_returns_none_when_no_flag_is_given(settings: Settings) -> None:
    """The signal to start the service. This is the exact call that used to
    raise ImportError before uvicorn was ever reached."""
    args = main.parse_args([])

    assert main._run_one_shot(args, settings) is None


def test_validate_config_exits_without_importing_discover(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(sys.modules, "discover", raising=False)

    assert main._run_one_shot(main.parse_args(["--validate-config"]), settings) == 0
    assert "discover" not in sys.modules


@pytest.mark.parametrize(
    ("flag", "function"),
    [
        ("--discover-accounts", "discover_accounts"),
        ("--discover-symbols", "discover_symbols"),
        ("--refresh-token", "refresh_token"),
    ],
)
def test_run_one_shot_dispatches_to_the_matching_discover_function(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    function: str,
) -> None:
    from execution_service import discover

    called: list[str] = []

    async def record(_settings: Settings) -> int:
        called.append(function)
        return 0

    monkeypatch.setattr(discover, function, record)

    assert main._run_one_shot(main.parse_args([flag]), settings) == 0
    assert called == [function]


def test_run_one_shot_propagates_the_exit_code(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execution_service import discover

    async def fail(_settings: Settings) -> int:
        return 1

    monkeypatch.setattr(discover, "discover_accounts", fail)

    assert main._run_one_shot(main.parse_args(["--discover-accounts"]), settings) == 1


def test_service_path_does_not_import_discover(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag check comes first so the long-running service never pays for the
    one-shot module."""
    monkeypatch.delitem(sys.modules, "discover", raising=False)

    main._run_one_shot(main.parse_args([]), settings)

    assert "discover" not in sys.modules


# --- run() -------------------------------------------------------------------


def test_run_starts_uvicorn_on_the_configured_host_and_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_env(tmp_path)
    monkeypatch.chdir(tmp_path)
    recorded: dict[str, Any] = {}

    def fake_run(app: Any, **kwargs: Any) -> None:
        recorded["app"] = app
        recorded.update(kwargs)

    monkeypatch.setattr(main.uvicorn, "run", fake_run)

    main.run(["--profile", "forex"])

    assert recorded["host"] == "127.0.0.1"
    assert recorded["port"] == 8010
    # One connection per process is the whole design; more workers would mean
    # more broker sessions on the same credentials.
    assert recorded["workers"] == 1
    assert recorded["factory"] is True


def test_run_exits_1_with_a_readable_message_when_the_env_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as caught:
        main.run(["--profile", "forex"])

    assert caught.value.code == 1
    assert ".env.forex" in capsys.readouterr().err


def test_run_reports_invalid_configuration_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Under launchd this is the difference between a diagnosable failure and a
    silent 60-second crash loop."""
    (tmp_path / ".env.forex").write_text(
        ENV_TEMPLATE.replace(
            "API_KEY=test-api-key-at-least-16",
            "API_KEY=replace-with-a-long-random-secret",
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as caught:
        main.run(["--profile", "forex"])

    assert caught.value.code == 1
    stderr = capsys.readouterr().err
    assert "Invalid configuration" in stderr
    # Named by its env-var alias, which is what the operator has to go and edit.
    assert "API_KEY" in stderr
    assert "placeholder" in stderr


def test_run_exits_with_the_one_shot_code_instead_of_starting_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execution_service import discover

    _write_env(tmp_path)
    monkeypatch.chdir(tmp_path)

    async def fail(_settings: Settings) -> int:
        return 3

    monkeypatch.setattr(discover, "discover_symbols", fail)

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("uvicorn must not start for a one-shot command")

    monkeypatch.setattr(main.uvicorn, "run", explode)

    with pytest.raises(SystemExit) as caught:
        main.run(["--profile", "forex", "--discover-symbols"])

    assert caught.value.code == 3
