from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from pydantic import (
    AliasChoices,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from ta_contracts import DEFAULT_SIGNAL_SOURCES
from ta_core import PLACEHOLDER_PREFIX, BaseServiceSettings, resolve_env_file
from ta_core import load_settings as _load_settings
from ta_notify import NotificationSettings
from ta_plugin_api import EXECUTION_GROUP, available, load_providers
from ta_plugin_binance_futures.settings import BinanceFuturesSettingsMixin
from ta_plugin_ctrader.accounts import (
    CTRADER_HOSTS,
    AccountDefinition,
    AccountRegistry,
    load_account_registry,
)
from ta_plugin_ctrader.settings import CTraderSettingsMixin, apply_account_registry
from ta_plugin_mt5.settings import MT5TerminalSettingsMixin
from ta_plugin_mt5.symbols import load_mt5_symbols

# Re-exported: main.py and the tests import these from here, and they are part
# of this module's surface even though the implementations moved to ta-core and
# to the broker plugins that own them.
__all__ = [
    "CTRADER_HOSTS",
    "AccountDefinition",
    "AccountRegistry",
    "Settings",
    "load_account_registry",
    "load_mt5_symbols",
    "load_settings",
    "resolve_env_file",
]


def load_settings(profile: str | None = None) -> Settings:
    """Load .env[.profile], then layer the account registry over it.

    The profile-scoped path defaults are a correctness requirement, not a
    convenience: two profiles sharing one token cache mutually invalidate each
    other's rotated refresh tokens, recoverable only by redoing the browser
    OAuth flow.
    """
    settings = _load_settings(
        Settings,
        profile,
        default_example=".env.example.forex",
        profile_scoped_paths={
            "token_cache_path": "data/token-cache.{profile}.json",
            "events_log_path": "logs/events.{profile}.jsonl",
            "execution_database_path": "data/executions.{profile}.sqlite3",
        },
    )
    if settings.accounts_config_path is not None:
        settings = apply_account_registry(settings)
        settings.validate_gateway_configuration()
    return settings


class Settings(
    BaseServiceSettings,
    NotificationSettings,
    CTraderSettingsMixin,
    MT5TerminalSettingsMixin,
    BinanceFuturesSettingsMixin,
):
    """api_key, host, log_level, events_log_path and profile come from the base.

    The NOTIFICATION_* fields come from ta-notify's mixin, which is the same set
    mt5-trader declared by hand. The CTRADER_*, transport and MT5_* terminal
    fields come from the broker plugins' mixins, so market-data-service binds
    them identically. What is left here is execution policy.
    """

    max_volume_lots: Decimal | None = Field(default=None, gt=0, validation_alias="MAX_VOLUME_LOTS")
    allowed_order_sources_csv: str = Field(default="", validation_alias="ALLOWED_ORDER_SOURCES")
    signal_max_age_seconds: int = Field(default=60, gt=0, validation_alias="SIGNAL_MAX_AGE_SECONDS")
    future_tolerance_seconds: int = Field(
        default=5, ge=0, validation_alias="FUTURE_TOLERANCE_SECONDS"
    )
    execution_response_timeout_seconds: float = Field(
        default=10.0, gt=0, validation_alias="EXECUTION_RESPONSE_TIMEOUT_SECONDS"
    )
    # How often UNKNOWN targets are re-checked against the broker; 0 disables.
    reconcile_interval_seconds: float = Field(
        default=60.0, ge=0, validation_alias="RECONCILE_INTERVAL_SECONDS"
    )
    execution_database_path: Path = Field(
        default=Path("data/executions.sqlite3"), validation_alias="EXECUTION_DATABASE_PATH"
    )

    # Overrides the base default of 8000; this service has always bound 8010.
    port: int = Field(default=8010, gt=0, le=65535, validation_alias="PORT")

    # --- adapter selection ---------------------------------------------------
    #
    # Which brokers this process talks to. The same codebase runs on macOS with
    # ADAPTERS=ctrader and on the Windows host with ADAPTERS=mt5; the adapter
    # module is imported lazily so a macOS install never touches MetaTrader5,
    # which has no wheel outside Windows.
    adapters_csv: str = Field(default="ctrader", validation_alias="ADAPTERS")

    # --- MetaTrader 5 --------------------------------------------------------
    #
    # Execution policy only; the terminal connection fields come from
    # MT5TerminalSettingsMixin. Required by validate_adapter_requirements when
    # mt5 is enabled, never otherwise: a cTrader-only host must still start.
    allowed_symbols_csv: str = Field(default="", validation_alias="ALLOWED_SYMBOLS")
    allowed_signal_sources_csv: str = Field(
        default=DEFAULT_SIGNAL_SOURCES,
        min_length=1,
        validation_alias="ALLOWED_SIGNAL_SOURCES",
    )
    maximum_volume: Decimal | None = Field(default=None, gt=0, validation_alias="MAXIMUM_VOLUME")
    magic_number: int = Field(default=0, ge=0, validation_alias="MAGIC_NUMBER")
    # The pre-unification MT5 signal ledger. Since execution unified on
    # EXECUTION_DATABASE_PATH this is read once, at startup, by the legacy
    # migration (and the OCO database still sits beside it); nothing writes it.
    database_path: Path = Field(default=Path("data/signals.db"), validation_alias="DATABASE_PATH")
    default_deviation_points: int = Field(
        default=10, ge=0, validation_alias="DEFAULT_DEVIATION_POINTS"
    )
    maximum_deviation_points: int = Field(
        default=20,
        ge=0,
        validation_alias=AliasChoices("MAXIMUM_DEVIATION_POINTS", "MAX_DEVIATION_POINTS"),
    )
    mt5_oco_enabled: bool = Field(default=False, validation_alias="MT5_OCO_ENABLED")
    mt5_oco_server_utc_offset_seconds: int = Field(
        default=0,
        ge=-50400,
        le=50400,
        validation_alias="MT5_OCO_SERVER_UTC_OFFSET_SECONDS",
    )
    mt5_oco_poll_seconds: float = Field(
        default=0.25, gt=0, le=5, validation_alias="MT5_OCO_POLL_SECONDS"
    )
    signals_log_path: Path = Field(
        default=Path("logs/signals.jsonl"), validation_alias="SIGNALS_LOG_PATH"
    )

    @field_validator("api_key", "notification_api_key")
    @classmethod
    def reject_placeholder(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and value.get_secret_value().startswith(PLACEHOLDER_PREFIX):
            raise ValueError(
                "still holds the .env.example placeholder value; replace it with a real secret"
            )
        return value

    @property
    def adapters(self) -> tuple[str, ...]:
        return tuple(
            token.strip().lower() for token in self.adapters_csv.split(",") if token.strip()
        )

    @property
    def allowed_symbols(self) -> frozenset[str]:
        """Case is preserved deliberately.

        MetaTrader 5 symbol lookup is case-sensitive and Deriv names them
        "Volatility 75 Index", "Step Index". Upper-casing here silently breaks
        every Deriv symbol.
        """
        return frozenset(
            symbol.strip() for symbol in self.allowed_symbols_csv.split(",") if symbol.strip()
        )

    @property
    def allowed_signal_sources(self) -> frozenset[str]:
        return frozenset(
            token.strip().lower()
            for token in self.allowed_signal_sources_csv.split(",")
            if token.strip()
        )

    @model_validator(mode="after")
    def colocate_mt5_event_log(self) -> Settings:
        """Keep an MT5 host's events.jsonl beside its signals.jsonl.

        mt5-trader derived it that way (`signals_log_path.parent / events.jsonl`)
        rather than from EVENTS_LOG_PATH, and operators' log tooling points at
        that directory. Only applies when EVENTS_LOG_PATH was not set explicitly.
        """
        if "mt5" in self.adapters and "events_log_path" not in self.model_fields_set:
            object.__setattr__(
                self, "events_log_path", self.signals_log_path.parent / "events.jsonl"
            )
        return self

    @model_validator(mode="after")
    def validate_adapter_requirements(self) -> Settings:
        """Require an adapter's configuration only when that adapter is enabled.

        The alternative — making the MetaTrader 5 fields unconditionally
        required — would stop a cTrader-only host from starting for want of a
        terminal path it will never use.
        """
        known = available(EXECUTION_GROUP)
        unknown = set(self.adapters) - known
        if unknown:
            raise ValueError(
                f"unknown ADAPTERS: {', '.join(sorted(unknown))}; "
                f"allowed: {', '.join(sorted(known))}"
            )
        if not self.adapters:
            raise ValueError("ADAPTERS must name at least one adapter")
        providers = load_providers(EXECUTION_GROUP, self.adapters)
        if "ctrader" in self.adapters:
            missing_ctrader = providers["ctrader"].missing_settings(self)
            if missing_ctrader:
                raise ValueError(
                    f"ADAPTERS includes ctrader, which requires: {', '.join(missing_ctrader)}"
                )
        if "mt5" in self.adapters:
            if self.symbols_file is not None and self.allowed_symbols:
                raise ValueError("Set either SYMBOLS_FILE or ALLOWED_SYMBOLS for MT5, not both")
            if self.symbols_file is not None:
                object.__setattr__(
                    self,
                    "allowed_symbols_csv",
                    ",".join(load_mt5_symbols(self.symbols_file)),
                )
            # Ported from mt5-trader's validate_defaults. These are not covered
            # by the missing-field check below because they constrain values
            # that have defaults.
            if self.default_deviation_points > self.maximum_deviation_points:
                raise ValueError("DEFAULT_DEVIATION_POINTS cannot exceed MAXIMUM_DEVIATION_POINTS")
            if not self.allowed_signal_sources:
                raise ValueError("ALLOWED_SIGNAL_SOURCES must contain at least one source slug")
            for source in self.allowed_signal_sources:
                if re.fullmatch(r"[a-z][a-z0-9_]*", source) is None:
                    raise ValueError(
                        f"ALLOWED_SIGNAL_SOURCES contains invalid slug {source!r}; "
                        "use lowercase letters, digits, and underscores"
                    )
            missing = providers["mt5"].missing_settings(self)
            if self.maximum_volume is None:
                missing.append("MAXIMUM_VOLUME")
            if not self.allowed_symbols:
                missing.append("ALLOWED_SYMBOLS or SYMBOLS_FILE")
            if missing:
                raise ValueError(f"ADAPTERS includes mt5, which requires: {', '.join(missing)}")
        if "binance_futures" in self.adapters:
            missing = providers["binance_futures"].missing_settings(self)
            if self.max_volume_lots is None:
                missing.append("MAX_VOLUME_LOTS")
            if not self.allowed_order_sources:
                missing.append("ALLOWED_ORDER_SOURCES")
            if missing:
                raise ValueError(
                    f"ADAPTERS includes binance_futures, which requires: {', '.join(missing)}"
                )
        return self

    def validate_gateway_configuration(self) -> None:
        if not self.accounts:
            raise ValueError("ACCOUNTS_CONFIG_PATH contains no accounts")
        if self.max_volume_lots is None:
            raise ValueError("MAX_VOLUME_LOTS is required with ACCOUNTS_CONFIG_PATH")
        if not self.allowed_order_sources:
            raise ValueError("ALLOWED_ORDER_SOURCES is required with ACCOUNTS_CONFIG_PATH")
        enabled_aliases = {account.alias for account in self.enabled_accounts}
        if self.default_market_data_account not in enabled_aliases:
            raise ValueError("DEFAULT_MARKET_DATA_ACCOUNT must name an enabled account")
        invalid_sources = sorted(
            source
            for source in self.allowed_order_sources
            if re.fullmatch(r"[a-z][a-z0-9_]*", source) is None
        )
        if invalid_sources:
            raise ValueError(
                f"ALLOWED_ORDER_SOURCES contains invalid source slugs: {invalid_sources}"
            )

    @property
    def source(self) -> str:
        if self.profile:
            return f"ctrader-markets.{self.profile}"
        return "ctrader-markets"

    @property
    def order_sources(self) -> frozenset[str]:
        """Who may call /v1/orders: ALLOWED_ORDER_SOURCES, or on an MT5-only
        host that never set it, the same sources /v1/signals accepts."""
        return self.allowed_order_sources or (
            self.allowed_signal_sources if "mt5" in self.adapters else frozenset()
        )

    @property
    def allowed_order_sources(self) -> frozenset[str]:
        return frozenset(
            value.strip().lower()
            for value in self.allowed_order_sources_csv.split(",")
            if value.strip()
        )
