"""Hard risk rules (plan §3), in code no model can reach.

``RiskLimits`` is frozen and built once from settings at startup. ``RiskState``
exposes no way to change a limit: callers can report facts (equity, positions,
book health) and ask questions (may this order go?), and an operator can kill
and acknowledge. A model output is never an argument to anything here.

Two kinds of stop:

- **Halt** (kill switch, daily loss, drawdown): cancel all, flatten, stop. Only
  an operator ``ack`` clears it; a daily-loss halt also waits for the next UTC
  day. While halted, only reduce-only orders pass.
- **Pause** (stale book, depth gap, stream down): per symbol, clears itself
  when the condition does. No new exposure while paused.

There is no order path yet. ``check_order`` and the governor exist so that
when one is built it cannot be built around them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from ta_plugin_binance_futures.limiter import OrderRateGovernor

__all__ = [
    "HaltReason",
    "OrderIntent",
    "RiskDecision",
    "RiskLimits",
    "RiskState",
]


class HaltReason(StrEnum):
    KILL = "kill_switch"
    DAILY_LOSS = "daily_loss_limit"
    DRAWDOWN = "max_drawdown"


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_position_notional_usd: float
    max_total_notional_usd: float
    daily_loss_limit_usd: float
    max_drawdown_pct: float
    manual_approval_notional_usd: float
    stale_book_ms: int
    order_rate_fraction: float

    @classmethod
    def from_settings(cls, settings: Any) -> RiskLimits:
        return cls(
            max_position_notional_usd=settings.max_position_notional_usd,
            max_total_notional_usd=settings.max_total_notional_usd,
            daily_loss_limit_usd=settings.daily_loss_limit_usd,
            max_drawdown_pct=settings.max_drawdown_pct,
            manual_approval_notional_usd=settings.manual_approval_notional_usd,
            stale_book_ms=settings.stale_book_ms,
            order_rate_fraction=settings.order_rate_fraction,
        )


@dataclass(frozen=True, slots=True)
class OrderIntent:
    symbol: str
    side: str  # "buy" | "sell"
    notional_usd: float  # positive
    reduce_only: bool = False


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reason: str
    needs_approval: bool = False


@dataclass
class _Halt:
    reason: HaltReason
    source: str
    at: datetime
    detail: str = ""


@dataclass
class RiskState:
    limits: RiskLimits
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    governor: OrderRateGovernor | None = None

    def __post_init__(self) -> None:
        if self.governor is None:
            self.governor = OrderRateGovernor(self.limits.order_rate_fraction)
        self.positions: dict[str, float] = {}  # signed notional, USD
        self.halt: _Halt | None = None
        self.pauses: dict[str, set[str]] = {}
        self.equity: float | None = None
        self.peak_equity: float | None = None
        self.day: date = self.clock().date()
        self.day_start_equity: float | None = None
        self.history: list[dict[str, Any]] = []

    # --- facts ------------------------------------------------------------------

    @property
    def halted(self) -> bool:
        return self.halt is not None

    def set_position(self, symbol: str, signed_notional_usd: float) -> None:
        self.positions[symbol] = signed_notional_usd

    def update_equity(self, equity: float) -> HaltReason | None:
        """Report account equity; returns a reason if this report caused a halt."""
        self._roll_day()
        self.equity = equity
        if self.day_start_equity is None:
            self.day_start_equity = equity
        self.peak_equity = equity if self.peak_equity is None else max(self.peak_equity, equity)
        if self.halt is not None:
            return None
        if self.day_start_equity - equity >= self.limits.daily_loss_limit_usd:
            return self._halt(
                HaltReason.DAILY_LOSS, "risk", f"down {self.day_start_equity - equity:.2f} today"
            )
        drawdown = (self.peak_equity - equity) / self.peak_equity * 100 if self.peak_equity else 0
        if drawdown >= self.limits.max_drawdown_pct:
            return self._halt(HaltReason.DRAWDOWN, "risk", f"{drawdown:.2f}% from peak")
        return None

    def set_pause(self, symbol: str, reason: str, active: bool) -> bool:
        """Raise or clear one pause reason; returns True if the state changed."""
        reasons = self.pauses.setdefault(symbol, set())
        if active and reason not in reasons:
            reasons.add(reason)
            return True
        if not active and reason in reasons:
            reasons.discard(reason)
            return True
        return False

    def paused(self, symbol: str) -> frozenset[str]:
        return frozenset(self.pauses.get(symbol, ()))

    # --- operator ---------------------------------------------------------------

    def kill(self, source: str, detail: str = "") -> bool:
        """Halt. Returns False if already halted (the first reason is kept)."""
        if self.halt is not None:
            return False
        self._halt(HaltReason.KILL, source, detail)
        return True

    def ack(self, source: str) -> tuple[bool, str]:
        """Clear a halt. A daily-loss halt stays until the next UTC day."""
        if self.halt is None:
            return False, "not halted"
        self._roll_day()
        if self.halt.reason is HaltReason.DAILY_LOSS and self.halt.at.date() >= self.day:
            return False, "daily loss halt clears only on the next UTC day"
        cleared = self.halt
        self.halt = None
        if cleared.reason is HaltReason.DRAWDOWN:
            # A fresh peak, or the next tick would halt again on the same drawdown.
            self.peak_equity = self.equity
        self.history.append(
            {
                "event": "ack",
                "source": source,
                "cleared": cleared.reason.value,
                "at": self.clock().isoformat(),
            }
        )
        return True, f"cleared {cleared.reason.value}"

    # --- questions --------------------------------------------------------------

    def check_order(self, intent: OrderIntent) -> RiskDecision:
        if intent.notional_usd <= 0:
            return RiskDecision(False, "non_positive_notional")
        if self.halt is not None and not intent.reduce_only:
            return RiskDecision(False, f"halted:{self.halt.reason.value}")
        pauses = self.paused(intent.symbol)
        if pauses and not intent.reduce_only:
            return RiskDecision(False, f"paused:{','.join(sorted(pauses))}")
        if not intent.reduce_only:
            signed = intent.notional_usd if intent.side == "buy" else -intent.notional_usd
            current = self.positions.get(intent.symbol, 0.0)
            after = current + signed
            if abs(after) > self.limits.max_position_notional_usd:
                return RiskDecision(False, "max_position_notional")
            total = sum(abs(v) for k, v in self.positions.items() if k != intent.symbol)
            if total + abs(after) > self.limits.max_total_notional_usd:
                return RiskDecision(False, "max_total_notional")
        needs_approval = intent.notional_usd > self.limits.manual_approval_notional_usd
        if needs_approval:
            # Waits for a human; it does not spend rate budget until approved.
            return RiskDecision(False, "manual_approval_required", needs_approval=True)
        assert self.governor is not None
        if not self.governor.try_acquire():
            return RiskDecision(False, "order_rate_governor")
        return RiskDecision(True, "ok")

    def snapshot(self) -> dict[str, Any]:
        return {
            "halted": self.halt is not None,
            "halt": None
            if self.halt is None
            else {
                "reason": self.halt.reason.value,
                "source": self.halt.source,
                "at": self.halt.at.isoformat(),
                "detail": self.halt.detail,
            },
            "pauses": {symbol: sorted(r) for symbol, r in self.pauses.items() if r},
            "positions": dict(self.positions),
            "equity": self.equity,
            "peak_equity": self.peak_equity,
            "day_start_equity": self.day_start_equity,
            "limits": {
                "max_position_notional_usd": self.limits.max_position_notional_usd,
                "max_total_notional_usd": self.limits.max_total_notional_usd,
                "daily_loss_limit_usd": self.limits.daily_loss_limit_usd,
                "max_drawdown_pct": self.limits.max_drawdown_pct,
                "manual_approval_notional_usd": self.limits.manual_approval_notional_usd,
                "stale_book_ms": self.limits.stale_book_ms,
            },
            "order_headroom": self.governor.headroom() if self.governor else None,
        }

    # --- internals --------------------------------------------------------------

    def _halt(self, reason: HaltReason, source: str, detail: str) -> HaltReason:
        self.halt = _Halt(reason, source, self.clock(), detail)
        self.history.append(
            {
                "event": "halt",
                "reason": reason.value,
                "source": source,
                "detail": detail,
                "at": self.halt.at.isoformat(),
            }
        )
        return reason

    def _roll_day(self) -> None:
        today = self.clock().date()
        if today != self.day:
            self.day = today
            self.day_start_equity = self.equity
