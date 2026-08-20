"""Sticky prop-firm guard evaluated on marked equity, including floating P&L."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from firm_profile import FirmProfile


@dataclass
class PropGuardState:
    breached: bool = False
    breach_reason: str | None = None
    breached_at: datetime | None = None
    daily_period: str | None = None
    daily_reference_equity: float | None = None
    last_equity_cash: float | None = None
    minimum_equity_cash: float | None = None


class PropGuard:
    def __init__(self, profile: FirmProfile | None) -> None:
        self.profile = profile
        self.state = PropGuardState()

    @property
    def enabled(self) -> bool:
        return self.profile is not None

    @property
    def blocks_new(self) -> bool:
        return self.state.breached

    def evaluate(self, ts: datetime, equity_cash: float) -> bool:
        """Update marked equity and return whether this call newly tripped the guard."""
        if self.profile is None:
            return False
        state = self.state
        period = _daily_period(ts, self.profile.timezone, self.profile.daily_reset_time)
        if state.daily_period is None:
            state.daily_period = period.isoformat()
            state.daily_reference_equity = self.profile.initial_balance
        elif state.daily_period != period.isoformat():
            state.daily_period = period.isoformat()
            state.daily_reference_equity = equity_cash
        state.last_equity_cash = equity_cash
        state.minimum_equity_cash = (
            equity_cash
            if state.minimum_equity_cash is None
            else min(state.minimum_equity_cash, equity_cash)
        )
        if state.breached:
            return False
        assert state.daily_reference_equity is not None
        daily_floor = state.daily_reference_equity * (
            1.0 - self.profile.daily_loss_limit_pct / 100.0
        )
        reason: str | None = None
        if equity_cash <= self.profile.total_equity_floor:
            reason = "total_loss_limit"
        elif equity_cash <= daily_floor:
            reason = "daily_loss_limit"
        if reason is None:
            return False
        state.breached = True
        state.breach_reason = reason
        state.breached_at = ts
        return True

    def snapshot(self) -> dict[str, object]:
        payload = asdict(self.state)
        breached_at = self.state.breached_at
        payload["breached_at"] = breached_at.isoformat() if breached_at else None
        return payload

    def restore(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        breached_at = payload.get("breached_at")
        self.state = PropGuardState(
            breached=bool(payload.get("breached", False)),
            breach_reason=(
                str(payload["breach_reason"]) if payload.get("breach_reason") else None
            ),
            breached_at=(datetime.fromisoformat(str(breached_at)) if breached_at else None),
            daily_period=(str(payload["daily_period"]) if payload.get("daily_period") else None),
            daily_reference_equity=_optional_float(payload.get("daily_reference_equity")),
            last_equity_cash=_optional_float(payload.get("last_equity_cash")),
            minimum_equity_cash=_optional_float(payload.get("minimum_equity_cash")),
        )


def _daily_period(ts: datetime, timezone: str, reset_time: str) -> date:
    local = ts.astimezone(ZoneInfo(timezone))
    reset = time.fromisoformat(reset_time)
    if local.timetz().replace(tzinfo=None) < reset:
        return local.date() - timedelta(days=1)
    return local.date()


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)
