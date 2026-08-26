from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from execution_service.config import load_settings, resolve_env_file
from tests.conftest import ENV_TEMPLATE, build_settings


def test_resolve_env_file_defaults_to_dotenv() -> None:
    assert resolve_env_file(None) == Path(".env")
    assert resolve_env_file("deriv") == Path(".env.deriv")


def test_load_settings_missing_profile_names_the_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError) as excinfo:
        load_settings("deriv")
    message = str(excinfo.value)
    assert ".env.deriv" in message
    assert ".env.example.deriv" in message


def test_load_settings_stamps_the_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.forex").write_text(ENV_TEMPLATE, encoding="utf-8")
    settings = load_settings("forex")
    assert settings.profile == "forex"
    assert settings.source == "ctrader-markets.forex"
    assert settings.symbols == frozenset({"EURUSD", "XAUUSD"})


def test_host_derived_from_environment(tmp_path: Path) -> None:
    assert build_settings(tmp_path).resolved_host == "demo.ctraderapi.com"
    live = build_settings(tmp_path, CTRADER_ENVIRONMENT="live")
    assert live.resolved_host == "live.ctraderapi.com"


def test_explicit_host_overrides_environment(tmp_path: Path) -> None:
    settings = build_settings(tmp_path, CTRADER_HOST="broker.example.com")
    assert settings.resolved_host == "broker.example.com"


def test_rejects_unknown_environment(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="demo or live"):
        build_settings(tmp_path, CTRADER_ENVIRONMENT="staging")


def test_heartbeat_interval_capped_below_the_protocol_timeout(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        build_settings(tmp_path, HEARTBEAT_INTERVAL_SECONDS=12)


def test_historical_rate_capped_at_the_documented_limit(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        build_settings(tmp_path, HISTORICAL_REQUESTS_PER_SECOND=25)


def test_rejects_empty_symbol_list(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        build_settings(tmp_path, SYMBOLS=" , ")


def test_rejects_backoff_inversion(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="RECONNECT_MAX_BACKOFF_SECONDS"):
        build_settings(
            tmp_path,
            RECONNECT_INITIAL_BACKOFF_SECONDS=90,
            RECONNECT_MAX_BACKOFF_SECONDS=60,
        )


def test_short_api_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        build_settings(tmp_path, API_KEY="too-short")


def test_symbols_preserve_case_and_strip_whitespace(tmp_path: Path) -> None:
    settings = build_settings(tmp_path, SYMBOLS=" US 500 , EURUSD ")
    assert settings.symbols == frozenset({"US 500", "EURUSD"})


@pytest.mark.parametrize(
    "field",
    ["CTRADER_CLIENT_ID", "CTRADER_CLIENT_SECRET", "CTRADER_ACCESS_TOKEN", "API_KEY"],
)
def test_rejects_unfilled_env_example_placeholder(tmp_path: Path, field: str) -> None:
    """API_KEY is the dangerous one: the template value is 32 characters, so it
    satisfies min_length=16 and would otherwise start the service with a secret
    that is published in this repository."""
    with pytest.raises(ValidationError, match="placeholder"):
        build_settings(tmp_path, **{field: "replace-with-a-long-random-secret"})


def test_accepts_a_real_secret_that_merely_contains_the_placeholder_words(
    tmp_path: Path,
) -> None:
    settings = build_settings(tmp_path, API_KEY="do-not-replace-with-anything-else")
    assert settings.api_key.get_secret_value() == "do-not-replace-with-anything-else"


# --- the shipped templates ---------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = sorted(REPO_ROOT.glob(".env.example.*"))


def _keys(path: Path) -> set[str]:
    return {
        line.split("=", 1)[0].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }


def test_the_examples_exist() -> None:
    assert {p.name for p in EXAMPLES} == {
        ".env.example.forex",
        ".env.example.deriv",
        ".env.example.production",
    }


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
def test_every_documented_key_is_a_real_setting(example: Path) -> None:
    """extra='ignore' means a stale or misspelled key in a template is silently
    dropped rather than rejected, so nothing else would catch this."""
    from execution_service.config import Settings

    aliases = {
        field.validation_alias
        for field in Settings.model_fields.values()
        if isinstance(field.validation_alias, str)
    }

    assert _keys(example) <= aliases, f"{example.name} documents unknown settings"


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
def test_an_unedited_template_is_rejected(
    example: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copying a template and starting the service must fail loudly rather than
    run with a placeholder API key that is published in this repository."""
    profile = example.name.rsplit(".", 1)[1]
    (tmp_path / f".env.{profile}").write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError) as caught:
        load_settings(profile)

    reported = {error["loc"][0] for error in caught.value.errors()}
    assert "API_KEY" in reported
    if profile == "production":
        assert "MAX_VOLUME_LOTS" in reported
    else:
        assert "CTRADER_ACCOUNT_ID" in reported


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
def test_each_template_scopes_its_writable_paths_to_its_profile(example: Path) -> None:
    """Two profiles sharing a token cache mutually invalidate each other's
    rotated refresh tokens, recoverable only by redoing the browser OAuth flow."""
    profile = example.name.rsplit(".", 1)[1]
    text = example.read_text(encoding="utf-8")

    assert f"TOKEN_CACHE_PATH=data/token-cache.{profile}.json" in text
    assert f"EVENTS_LOG_PATH=logs/events.{profile}.jsonl" in text


def test_an_unset_writable_path_still_defaults_per_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The safety net for a deployer who deletes those lines."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.deriv").write_text(ENV_TEMPLATE, encoding="utf-8")

    settings = load_settings("deriv")

    assert settings.token_cache_path == Path("data/token-cache.deriv.json")
    assert settings.events_log_path == Path("logs/events.deriv.jsonl")


def test_an_explicit_path_is_not_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.deriv").write_text(
        ENV_TEMPLATE + "TOKEN_CACHE_PATH=/secrets/custom.json\n", encoding="utf-8"
    )

    assert load_settings("deriv").token_cache_path == Path("/secrets/custom.json")
