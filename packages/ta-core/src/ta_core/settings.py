"""The settings every service shares, plus the `.env.<profile>` loader.

`BaseServiceSettings` carries only what all of them genuinely had in common:
the API key, the bind address, the log level, the events log, and the profile
name. Everything broker- or strategy-specific stays in the service's own
subclass.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TypeVar

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Every secret in .env.example.* starts with this. A value that still carries it
# means the template was copied but never filled in — which for API_KEY would
# otherwise start the service with a key published in this repository.
PLACEHOLDER_PREFIX = "replace-with-"

_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def resolve_env_file(
    profile: str | None,
    *,
    settings_class: type | None = None,
) -> Path:
    """Return `.env` or `.env.<profile>`.

    Looks in the current working directory first. When the cwd is this
    repository's uv workspace root (so `execution-service --profile forex` from
    `trading-algos/` works), also looks in `services/<dist-name>/`.
    """
    name = ".env" if profile is None else f".env.{profile}"
    cwd_path = Path.cwd() / name
    if cwd_path.is_file():
        return cwd_path
    extra = _workspace_member_dir(settings_class) if settings_class is not None else None
    if extra is not None:
        candidate = extra / name
        if candidate.is_file():
            return candidate
    return Path(name)


def _is_uv_workspace_root(path: Path) -> bool:
    pyproject = path / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return False
    return "[tool.uv.workspace]" in text


def _workspace_member_dir(settings_class: type) -> Path | None:
    cwd = Path.cwd().resolve()
    if not _is_uv_workspace_root(cwd):
        return None
    top = settings_class.__module__.split(".", 1)[0]
    if top in {"ta_core", "tests"}:
        return None
    slug = top.replace("_", "-")
    candidate = cwd / "services" / slug
    return candidate if candidate.is_dir() else None


class BaseServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    api_key: SecretStr = Field(min_length=16, validation_alias="API_KEY")
    host: str = Field(default="127.0.0.1", min_length=1, validation_alias="HOST")
    port: int = Field(default=8000, gt=0, le=65535, validation_alias="PORT")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    events_log_path: Path = Field(
        default=Path("logs/events.jsonl"), validation_alias="EVENTS_LOG_PATH"
    )
    profile: str | None = None

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in _LOG_LEVELS:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        return normalized

    @field_validator("api_key")
    @classmethod
    def reject_placeholder(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and value.get_secret_value().startswith(PLACEHOLDER_PREFIX):
            raise ValueError(
                "still holds the .env.example placeholder value; replace it with a real secret"
            )
        return value


SettingsT = TypeVar("SettingsT", bound=BaseServiceSettings)


def load_settings(
    settings_class: type[SettingsT],
    profile: str | None = None,
    *,
    default_example: str = ".env.example.forex",
    profile_scoped_paths: dict[str, str] | None = None,
) -> SettingsT:
    """Load `.env` or `.env.<profile>` into `settings_class`.

    `profile_scoped_paths` maps a field name to a `str.format`-style template
    taking `{profile}`; a listed field is redirected only when the env file did
    not set it explicitly. Two profiles sharing one token cache mutually
    invalidate each other's rotated refresh tokens, recoverable only by redoing
    the browser OAuth flow — so per-profile defaults are a correctness
    requirement, not a convenience.
    """
    env_file = resolve_env_file(profile, settings_class=settings_class)
    if not env_file.is_file():
        hint = f".env.example.{profile}" if profile else default_example
        raise FileNotFoundError(f"Missing {env_file}. Copy {hint} and configure it.")
    env_parent = env_file.expanduser().resolve().parent
    if env_parent != Path.cwd().resolve():
        # Relative paths in the env file (token cache, sqlite, account registry)
        # are resolved against the process cwd. Match the directory that owns
        # the file so `execution-service --profile forex` from the workspace
        # root still writes data/ and logs/ next to the service.
        os.chdir(env_parent)
    settings = settings_class(_env_file=env_file, _env_file_encoding="utf-8")

    update: dict[str, object] = {"profile": profile}
    if profile is not None:
        for field, template in (profile_scoped_paths or {}).items():
            if field not in settings.model_fields_set:
                update[field] = Path(template.format(profile=profile))
    return settings.model_copy(update=update)
