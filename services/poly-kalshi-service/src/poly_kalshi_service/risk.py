"""Hard limits on paper entries: open notional, daily loss, the kill switch.

A halt (``/kill``, ``POST /v1/kill`` or the kill file) blocks new entries until
acknowledged over HTTP; it never stops recording or decision logging, since a
blocked decision is still evidence. Positions are never closed early: there
is nothing to flatten in a hold-to-settlement strategy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ta_core.logging_config import log_event

from .ledger import Ledger

__all__ = ["RiskLimits", "RiskState"]


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_open_notional_usd: float
    daily_loss_limit_usd: float
    kill_file: Path | None = None


class RiskState:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits
        self.halted = False
        self.halt_reason: str | None = None

    def kill(self, source: str, reason: str = "") -> bool:
        fired = not self.halted
        self.halted = True
        self.halt_reason = f"{source}: {reason}".strip(": ")
        log_event("risk_halt", level=logging.WARNING, source=source, reason=reason, fired=fired)
        return fired

    def kill_file_present(self) -> bool:
        return self.limits.kill_file is not None and self.limits.kill_file.exists()

    def ack(self) -> tuple[bool, str]:
        if self.kill_file_present():
            return False, f"kill file {self.limits.kill_file} still exists"
        self.halted = False
        self.halt_reason = None
        log_event("risk_ack")
        return True, "cleared"

    def check(self, ledger: Ledger, order_cost: float, t_ns: int) -> str | None:
        """None if the entry may go ahead, else why not."""
        if self.kill_file_present() and not self.halted:
            self.kill("kill_file", str(self.limits.kill_file))
        if self.halted:
            return "halted"
        if ledger.realised_today(t_ns) <= -self.limits.daily_loss_limit_usd:
            return "daily_loss_limit"
        if ledger.open_notional() + order_cost > self.limits.max_open_notional_usd + 1e-9:
            return "max_open_notional"
        if not ledger.can_afford(order_cost):
            return "insufficient_cash"
        return None

    def snapshot(self) -> dict[str, Any]:
        return {
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "kill_file_present": self.kill_file_present(),
            "max_open_notional_usd": self.limits.max_open_notional_usd,
            "daily_loss_limit_usd": self.limits.daily_loss_limit_usd,
        }
