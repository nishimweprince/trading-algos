from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from pydantic import Field, SecretStr, ValidationError

from ta_core import (
    BaseServiceSettings,
    ServiceError,
    configure_file_logs,
    configure_logging,
    create_base_app,
    load_settings,
    log_event,
    reset_file_logs,
    resolve_env_file,
)

API_KEY = "0123456789abcdef0123"


class DemoSettings(BaseServiceSettings):
    widget: str = Field(default="none", validation_alias="WIDGET")
    cache_path: Path = Field(default=Path("data/cache.json"), validation_alias="CACHE_PATH")


def make_settings(**overrides: object) -> DemoSettings:
    return DemoSettings(api_key=SecretStr(API_KEY), **overrides)  # type: ignore[arg-type]


# --- settings ---------------------------------------------------------------


def test_resolve_env_file_uses_profile_suffix() -> None:
    assert resolve_env_file(None) == Path(".env")
    assert resolve_env_file("forex") == Path(".env.forex")


def test_load_settings_reports_the_example_to_copy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError) as excinfo:
        load_settings(DemoSettings, "deriv")
    assert ".env.deriv" in str(excinfo.value)
    assert ".env.example.deriv" in str(excinfo.value)


def test_load_settings_scopes_unset_paths_to_the_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    Path(".env.forex").write_text(f"API_KEY={API_KEY}\n", encoding="utf-8")
    settings = load_settings(
        DemoSettings, "forex", profile_scoped_paths={"cache_path": "data/cache.{profile}.json"}
    )
    assert settings.profile == "forex"
    assert settings.cache_path == Path("data/cache.forex.json")


def test_load_settings_leaves_explicitly_set_paths_alone(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    Path(".env.forex").write_text(
        f"API_KEY={API_KEY}\nCACHE_PATH=data/mine.json\n", encoding="utf-8"
    )
    settings = load_settings(
        DemoSettings, "forex", profile_scoped_paths={"cache_path": "data/cache.{profile}.json"}
    )
    assert settings.cache_path == Path("data/mine.json")


def test_placeholder_api_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        DemoSettings(api_key=SecretStr("replace-with-a-real-key-here"))


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError, match="LOG_LEVEL"):
        make_settings(log_level="chatty")


def test_short_api_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DemoSettings(api_key=SecretStr("tooshort"))


# --- app factory ------------------------------------------------------------


def build_app(ready: bool = True):
    settings = make_settings()
    app, auth = create_base_app(
        settings,
        title="demo",
        readiness=lambda: (ready, {"detail": "probe"}),
    )

    @app.get("/v1/thing", dependencies=[Depends(auth)])
    async def thing() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/v1/boom", dependencies=[Depends(auth)])
    async def boom() -> dict[str, str]:
        raise ServiceError(409, "conflict", "already exists", {"id": 1})

    return app


def test_liveness_needs_no_key() -> None:
    with TestClient(build_app()) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "details": None}


def test_readiness_reports_503_when_not_ready() -> None:
    with TestClient(build_app(ready=False)) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "details": {"detail": "probe"}}


def test_missing_key_is_rejected() -> None:
    with TestClient(build_app()) as client:
        response = client.get("/v1/thing")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_wrong_key_is_rejected() -> None:
    with TestClient(build_app()) as client:
        response = client.get("/v1/thing", headers={"X-API-Key": "0" * 20})
    assert response.status_code == 401


def test_correct_key_is_accepted() -> None:
    with TestClient(build_app()) as client:
        response = client.get("/v1/thing", headers={"X-API-Key": API_KEY})
    assert response.status_code == 200


def test_service_error_renders_the_error_envelope() -> None:
    with TestClient(build_app()) as client:
        response = client.get("/v1/boom", headers={"X-API-Key": API_KEY})
    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "conflict", "message": "already exists", "details": {"id": 1}}
    }


# --- logging ----------------------------------------------------------------


def test_log_event_writes_console_events_to_the_file_sink(tmp_path: Path) -> None:
    """ctrader's contract: the durable record gets everything, not just console=False."""
    path = tmp_path / "logs" / "events.jsonl"
    configure_logging("INFO", name="test.events")
    configure_file_logs(path)
    try:
        log_event("connected", console=True, host="demo")
        log_event("tick", console=False, symbol="XAUUSD")
    finally:
        reset_file_logs()

    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["connected", "tick"]
    assert events[0]["host"] == "demo"
    assert events[1]["symbol"] == "XAUUSD"


def test_file_sink_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    configure_file_logs(path)
    try:
        log_event("anything")
    finally:
        reset_file_logs()
    assert (path.stat().st_mode & 0o777) == 0o600


def test_append_survives_a_deleted_log_directory(tmp_path: Path) -> None:
    """The reader loop must not be able to die because logs/ was rotated away."""
    directory = tmp_path / "logs"
    path = directory / "events.jsonl"
    sink = configure_file_logs(path)
    try:
        log_event("first")
        shutil.rmtree(directory)
        log_event("second")
    finally:
        reset_file_logs()

    assert sink.path.is_file()
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["second"]


def test_log_event_level_name_is_recorded(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    configure_file_logs(path)
    try:
        log_event("uh_oh", level=logging.WARNING)
    finally:
        reset_file_logs()
    event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert event["level"] == "WARNING"
