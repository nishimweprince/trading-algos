"""Settings: the provider plugins' mixins, plus which market each one serves.

``MARKETS_CONFIG_PATH`` names a TOML file with one table per enabled market::

    [forex]
    provider = "ctrader"
    feed = "forex_demo"   # a cTrader account alias; omit for an MT5 terminal

The providers this process loads are exactly the ones that file names.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from ta_contracts import MarketKind
from ta_core import BaseServiceSettings, resolve_env_file
from ta_core import load_settings as _load_settings
from ta_plugin_api import MARKET_DATA_GROUP, load_providers
from ta_plugin_ctrader.settings import CTraderSettingsMixin, apply_account_registry
from ta_plugin_mt5.settings import MT5TerminalSettingsMixin

from .markets import validate_binding

__all__ = [
    "MarketBinding",
    "Settings",
    "load_markets",
    "load_settings",
    "resolve_env_file",
]


class MarketBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1)
    feed: str | None = Field(default=None, min_length=1)


class Settings(BaseServiceSettings, CTraderSettingsMixin, MT5TerminalSettingsMixin):
    """api_key, host, log_level, events_log_path and profile come from the base;
    CTRADER_*, transport and MT5_* terminal fields from the plugins' mixins."""

    # 8020 on the macOS cTrader host; 8021+ for the per-terminal Windows profiles.
    port: int = Field(default=8020, gt=0, le=65535, validation_alias="PORT")

    markets_config_path: Path | None = Field(default=None, validation_alias="MARKETS_CONFIG_PATH")
    markets: dict[MarketKind, MarketBinding] = Field(default_factory=dict)

    max_candles_lookback: int = Field(default=5000, gt=0, validation_alias="MAX_CANDLES_LOOKBACK")
    sse_keepalive_seconds: float = Field(
        default=15.0, gt=0, validation_alias="SSE_KEEPALIVE_SECONDS"
    )
    # /health/ready reports not_ready when a market's newest quote is older.
    tick_staleness_seconds: float = Field(
        default=60.0, gt=0, validation_alias="TICK_STALENESS_SECONDS"
    )

    @model_validator(mode="after")
    def view_only(self) -> Self:
        """This process never trades. The flags exist only because the cTrader
        gateway reports them; pin them off whatever the environment says."""
        object.__setattr__(self, "trading_enabled", False)
        object.__setattr__(self, "live_trading_enabled", False)
        return self

    @model_validator(mode="after")
    def validate_markets(self) -> Self:
        self.check_markets()
        return self

    def check_markets(self) -> None:
        for market, binding in self.markets.items():
            validate_binding(market, binding.provider)

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(sorted({binding.provider for binding in self.markets.values()}))

    def validate_providers(self) -> None:
        """Discover every named provider and check it has its configuration.

        Separate from field validation because it imports plugins; tests that
        inject providers skip it.
        """
        if not self.markets:
            raise ValueError("MARKETS_CONFIG_PATH must enable at least one market")
        factories = load_providers(MARKET_DATA_GROUP, self.providers)
        for name, factory in factories.items():
            missing = factory.missing_settings(self)
            if missing:
                raise ValueError(f"provider {name} requires: {', '.join(missing)}")


def load_markets(path: Path) -> dict[MarketKind, MarketBinding]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing markets config {path}")
    with path.open("rb") as handle:
        raw: dict[str, Any] = tomllib.load(handle)
    unknown = sorted(set(raw) - {kind.value for kind in MarketKind})
    if unknown:
        raise ValueError(
            f"{path} names unknown markets {unknown}; "
            f"known: {', '.join(kind.value for kind in MarketKind)}"
        )
    try:
        return {
            MarketKind(name): MarketBinding.model_validate(table) for name, table in raw.items()
        }
    except ValidationError as exc:
        raise ValueError(f"Invalid market binding in {path}: {exc}") from exc


def load_settings(profile: str | None = None) -> Settings:
    """Load .env[.profile], then the cTrader account registry and the markets.

    Token cache and event log default to per-profile paths: two processes that
    share a token cache invalidate each other's rotated refresh tokens.
    """
    settings = _load_settings(
        Settings,
        profile,
        default_example=".env.example.ctrader",
        profile_scoped_paths={
            "token_cache_path": "data/token-cache.{profile}.json",
            "events_log_path": "logs/events.{profile}.jsonl",
        },
    )
    settings = apply_account_registry(settings)
    if settings.markets_config_path is None:
        raise ValueError("MARKETS_CONFIG_PATH is required")
    settings = settings.model_copy(update={"markets": load_markets(settings.markets_config_path)})
    settings.check_markets()
    settings.validate_providers()
    return settings
