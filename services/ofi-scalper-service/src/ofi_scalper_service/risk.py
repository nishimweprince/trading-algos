"""Hard risk rules (plan §3), in code no model can reach.

``RiskLimits`` is frozen and built once from settings at startup. ``RiskState``
exposes no way to change a limit: callers can report facts (equity, positions,
book health) and ask questions (may this order go?), and an operator can kill
and acknowledge. A model output is never an argument to anything here.

Two kinds of stop:

- **Halt** (kill switch; daily loss and drawdown when ``enforce_loss_limits``):
  close everything and stop. Only an operator ``ack`` clears it; a daily-loss
  halt also waits for the next UTC day. While halted, only reduce-only orders
  pass.
- **Breach** (daily loss and drawdown when not ``enforce_loss_limits``, as in
  the shadow service): recorded once per UTC day or drawdown episode, never a
  stop. Research enforces the limits so its gates mean something.
- **Pause** (stale book, depth gap, stream down): per symbol, clears itself
  when the condition does. No new exposure while paused.

With a ``state_path`` the halt and the recent halt/ack history are saved there
on every change and loaded at construction, so a halt survives a restart. An
unreadable file starts halted. Replay and backtest pass no path.

An order-path fault (closes that keep failing) is recorded with
``record_fault``; it does not halt. The execution bridge runs every order past
``check_order`` (and so the governor) before it is sent, and reacts to a halt
by closing every cycle.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ta_plugin_binance_futures.limiter import OrderRateGovernor

__all__ = [
    "HaltReason",
    "OrderIntent",
    "RiskDecision",
    "RiskLimits",
    "RiskState",
]

STATE_SCHEMA = 1
HISTORY_KEPT = 50


class HaltReason(StrEnum):
    KILL = "kill_switch"
    DAILY_LOSS = "daily_loss_limit"
    DRAWDOWN = "max_drawdown"
    # Closes that keep failing: recorded by record_fault, never a halt.
    EXECUTION = "execution_fault"


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
    state_path: Path | None = None
    enforce_loss_limits: bool = True

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
        # True while the halt in force was loaded from state_path, not raised here.
        self.restored = False
        self.persist_error: str | None = None
        self.breaches: list[dict[str, Any]] = []  # today's, record-only mode
        self._new_breaches: list[dict[str, Any]] = []
        self._daily_breached = False
        self._drawdown_breached = False
        if self.state_path is not None:
            self._load()

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
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity
            self._drawdown_breached = False  # a new peak starts a new episode
        if self.halt is not None and self.enforce_loss_limits:
            return None
        if self.day_start_equity - equity >= self.limits.daily_loss_limit_usd:
            detail = f"down {self.day_start_equity - equity:.2f} today"
            if self.enforce_loss_limits:
                return self._halt(HaltReason.DAILY_LOSS, "risk", detail)
            if not self._daily_breached:
                self._daily_breached = True
                self._breach(HaltReason.DAILY_LOSS, detail)
        drawdown = (self.peak_equity - equity) / self.peak_equity * 100 if self.peak_equity else 0
        if drawdown >= self.limits.max_drawdown_pct:
            detail = f"{drawdown:.2f}% from peak"
            if self.enforce_loss_limits:
                return self._halt(HaltReason.DRAWDOWN, "risk", detail)
            if not self._drawdown_breached:
                self._drawdown_breached = True
                self._breach(HaltReason.DRAWDOWN, detail)
        return None

    def take_breaches(self) -> list[dict[str, Any]]:
        """Breaches recorded since the last call (record-only mode)."""
        new, self._new_breaches = self._new_breaches, []
        return new

    def record_fault(self, detail: str) -> dict[str, Any]:
        """An order-path fault: recorded in the history, never a halt."""
        event = {
            "event": "fault",
            "reason": HaltReason.EXECUTION.value,
            "source": "bridge",
            "detail": detail,
            "at": self.clock().isoformat(),
        }
        self.history.append(event)
        self._save()
        return event

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
        self.restored = False
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
        self._save()
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
                "restored": self.restored,
            },
            "persist_error": self.persist_error,
            "enforce_loss_limits": self.enforce_loss_limits,
            "breaches": list(self.breaches),
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
        self._save()
        return reason

    def _breach(self, reason: HaltReason, detail: str) -> None:
        event = {
            "event": "breach",
            "reason": reason.value,
            "source": "risk",
            "detail": detail,
            "enforced": False,
            "at": self.clock().isoformat(),
        }
        self.history.append(event)
        self.breaches.append(event)
        self._new_breaches.append(event)
        self._save()

    def _load(self) -> None:
        assert self.state_path is not None
        if not self.state_path.exists():
            return
        try:
            doc = json.loads(self.state_path.read_text())
            halt = doc.get("halt")
            history = doc.get("history") or []
            if halt is not None:
                self.halt = _Halt(
                    HaltReason(halt["reason"]),
                    str(halt["source"]),
                    datetime.fromisoformat(halt["at"]),
                    str(halt.get("detail") or ""),
                )
                self.restored = True
            self.history = [dict(h) for h in history][-HISTORY_KEPT:]
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            # Fail closed: a halt we cannot read might have been a kill.
            self.halt = _Halt(
                HaltReason.KILL, "state", self.clock(), f"unreadable {self.state_path}: {exc}"
            )
            self.restored = True

    def _save(self) -> None:
        if self.state_path is None:
            return
        halt = self.halt
        doc = {
            "schema": STATE_SCHEMA,
            "updated_at": self.clock().isoformat(),
            "halt": None
            if halt is None
            else {
                "reason": halt.reason.value,
                "source": halt.source,
                "at": halt.at.isoformat(),
                "detail": halt.detail,
            },
            "history": self.history[-HISTORY_KEPT:],
        }
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(doc, indent=1))
            os.replace(tmp, self.state_path)
            self.persist_error = None
        except OSError as exc:
            # The halt still holds in memory; status shows the save failed.
            self.persist_error = f"{type(exc).__name__}: {exc}"

    def _roll_day(self) -> None:
        today = self.clock().date()
        if today != self.day:
            self.day = today
            self.day_start_equity = self.equity
            self.breaches = []
            self._daily_breached = False
