"""Explicit firm-limit profile used by PropGuard."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FirmProfile:
    initial_balance: float
    daily_loss_limit_pct: float
    total_loss_limit_pct: float
    timezone: str
    daily_reset_time: str

    @property
    def total_equity_floor(self) -> float:
        return self.initial_balance * (1.0 - self.total_loss_limit_pct / 100.0)
