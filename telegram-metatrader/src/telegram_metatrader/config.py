from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - only happens before dependencies are installed
    load_dotenv = None


DEFAULT_SYMBOL_MAP = {
    "GOLD": "XAUUSD",
    "XAU": "XAUUSD",
    "XAUUSD": "XAUUSD",
}


@dataclass(frozen=True)
class AppConfig:
    telegram_api_id: int
    telegram_api_hash: str
    telegram_phone_number: str | None
    telegram_session_path: Path
    telegram_session_string: str | None
    chat_id: str
    mt5_terminal_path: str | None
    mt5_login: int | None
    mt5_password: str | None
    mt5_server: str | None
    mt5_timeout_ms: int
    live_trading_enabled: bool
    default_volume: float
    max_deviation_points: int
    order_magic: int
    symbol_map: dict[str, str]
    poll_interval_seconds: float
    poll_max_messages: int
    state_dir: Path
    logs_dir: Path
    log_level: str
    log_raw_messages: bool
    http_host: str
    http_port: int

    @property
    def cursor_path(self) -> Path:
        return self.state_dir / "cursor.json"

    @property
    def handled_path(self) -> Path:
        return self.state_dir / "handled.json"


def load_config(cwd: Path | str | None = None, env: Mapping[str, str] | None = None) -> AppConfig:
    root = Path(cwd or os.getcwd())
    if env is None and load_dotenv is not None:
        load_dotenv(root / ".env")
    values = dict(os.environ if env is None else env)

    telegram_api_id = _required_int(values, "TELEGRAM_API_ID")
    telegram_api_hash = _required(values, "TELEGRAM_API_HASH")
    chat_id = _required(values, "CHAT_ID")

    state_dir = _path(root, values.get("STATE_DIR", "./state"))
    logs_dir = _path(root, values.get("LOGS_DIR", "./logs"))
    session_path = _path(root, values.get("TELEGRAM_SESSION_PATH", "./state/telegram.session"))

    return AppConfig(
        telegram_api_id=telegram_api_id,
        telegram_api_hash=telegram_api_hash,
        telegram_phone_number=_optional(values, "TELEGRAM_PHONE_NUMBER"),
        telegram_session_path=session_path,
        telegram_session_string=_optional(values, "TELEGRAM_SESSION_STRING"),
        chat_id=chat_id.strip(),
        mt5_terminal_path=_optional(values, "MT5_TERMINAL_PATH"),
        mt5_login=_optional_int(values, "MT5_LOGIN"),
        mt5_password=_optional(values, "MT5_PASSWORD"),
        mt5_server=_optional(values, "MT5_SERVER"),
        mt5_timeout_ms=_int(values, "MT5_TIMEOUT_MS", 60000, minimum=1000),
        live_trading_enabled=_bool(values.get("LIVE_TRADING_ENABLED"), default=False),
        default_volume=_float(values, "DEFAULT_VOLUME", 0.01, minimum=0.0, exclusive_minimum=True),
        max_deviation_points=_int(values, "MAX_DEVIATION_POINTS", 20, minimum=0),
        order_magic=_int(values, "ORDER_MAGIC", 240710, minimum=0),
        symbol_map=_symbol_map(values.get("SYMBOL_MAP_JSON")),
        poll_interval_seconds=_float(values, "POLL_INTERVAL_SECONDS", 5.0, minimum=0.5),
        poll_max_messages=_int(values, "POLL_MAX_MESSAGES", 50, minimum=1, maximum=500),
        state_dir=state_dir,
        logs_dir=logs_dir,
        log_level=values.get("LOG_LEVEL", "INFO").upper(),
        log_raw_messages=_bool(values.get("LOG_RAW_MESSAGES"), default=False),
        http_host=values.get("HTTP_HOST", "127.0.0.1"),
        http_port=_int(values, "HTTP_PORT", 8091, minimum=1, maximum=65535),
    )


def resolve_symbol(symbol_map: Mapping[str, str], parsed_symbol: str) -> str | None:
    key = parsed_symbol.upper().replace("/", "").strip()
    if key in symbol_map:
        return symbol_map[key]
    return None


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _required_int(values: Mapping[str, str], name: str) -> int:
    return _parse_int(_required(values, name), name)


def _optional(values: Mapping[str, str], name: str) -> str | None:
    value = values.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _optional_int(values: Mapping[str, str], name: str) -> int | None:
    value = _optional(values, name)
    return None if value is None else _parse_int(value, name)


def _int(
    values: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = values.get(name)
    value = default if raw is None or raw.strip() == "" else _parse_int(raw, name)
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def _float(
    values: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    exclusive_minimum: bool = False,
) -> float:
    raw = values.get(name)
    try:
        value = default if raw is None or raw.strip() == "" else float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if minimum is not None:
        if exclusive_minimum and value <= minimum:
            raise ValueError(f"{name} must be > {minimum}")
        if not exclusive_minimum and value < minimum:
            raise ValueError(f"{name} must be >= {minimum}")
    return value


def _parse_int(raw: str, name: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _bool(raw: str | None, *, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {raw}")


def _symbol_map(raw: str | None) -> dict[str, str]:
    if raw is None or raw.strip() == "":
        return dict(DEFAULT_SYMBOL_MAP)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("SYMBOL_MAP_JSON must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("SYMBOL_MAP_JSON must be a JSON object")
    out: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str) or not key.strip() or not value.strip():
            raise ValueError("SYMBOL_MAP_JSON keys and values must be non-empty strings")
        out[key.upper().replace("/", "").strip()] = value.strip()
    return out


def _path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path

