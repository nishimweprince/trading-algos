"""Explicit firm-limit profile used by PropGuard."""

from __future__ import annotations

from dataclasses import dataclass

CUSTOM_FIRM_PROFILE_NAME = "session-hedging-custom"
CUSTOM_FIRM_PROFILE_VERSION = "1.0"
DISABLED_FIRM_PROFILE_NAME = "none"


@dataclass(frozen=True)
class FirmProfile:
    initial_balance: float
    daily_loss_limit_pct: float
    total_loss_limit_pct: float
    timezone: str
    daily_reset_time: str
    name: str = CUSTOM_FIRM_PROFILE_NAME
    version: str = CUSTOM_FIRM_PROFILE_VERSION

    @property
    def total_equity_floor(self) -> float:
        return self.initial_balance * (1.0 - self.total_loss_limit_pct / 100.0)


def firm_identity(enabled: bool) -> tuple[str, str | None]:
    """Return the identifiable name and version for report headers."""
    if enabled:
        return CUSTOM_FIRM_PROFILE_NAME, CUSTOM_FIRM_PROFILE_VERSION
    return DISABLED_FIRM_PROFILE_NAME, None
