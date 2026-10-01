"""The cTrader account registry: which ctidTraderAccountIds a host may use.

Parsed from the TOML file named by ``ACCOUNTS_CONFIG_PATH``. Each account maps
canonical instrument names to the exact broker symbol names on that account.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "CTRADER_HOSTS",
    "AccountDefinition",
    "AccountRegistry",
    "load_account_registry",
]

CTRADER_HOSTS = {
    "demo": "demo.ctraderapi.com",
    "live": "live.ctraderapi.com",
}


class AccountDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alias: str = Field(min_length=1, max_length=63, pattern=r"^[a-z][a-z0-9_-]*$")
    ctid_trader_account_id: int = Field(gt=0)
    environment: str
    enabled: bool = True
    instruments: dict[str, str] = Field(min_length=1)

    @field_validator("environment")
    @classmethod
    def normalize_environment(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in CTRADER_HOSTS:
            raise ValueError("account environment must be demo or live")
        return normalized

    @field_validator("instruments")
    @classmethod
    def normalize_instruments(cls, value: dict[str, str]) -> dict[str, str]:
        normalized = {
            canonical.strip().upper(): broker_symbol.strip()
            for canonical, broker_symbol in value.items()
            if canonical.strip() and broker_symbol.strip()
        }
        if not normalized:
            raise ValueError("account instruments must not be empty")
        return normalized


class AccountRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    default_market_data_account: str
    accounts: tuple[AccountDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_registry(self) -> AccountRegistry:
        aliases = [account.alias for account in self.accounts]
        ids = [account.ctid_trader_account_id for account in self.accounts]
        if len(aliases) != len(set(aliases)):
            raise ValueError("account aliases must be unique")
        if len(ids) != len(set(ids)):
            raise ValueError("ctidTraderAccountIds must be unique")
        enabled = {account.alias for account in self.accounts if account.enabled}
        if self.default_market_data_account not in enabled:
            raise ValueError("default_market_data_account must name an enabled account")
        return self


def load_account_registry(path: Path) -> AccountRegistry:
    if not path.is_file():
        raise FileNotFoundError(f"Missing account registry {path}")
    with path.open("rb") as handle:
        raw: dict[str, Any] = tomllib.load(handle)
    return AccountRegistry.model_validate(raw)
