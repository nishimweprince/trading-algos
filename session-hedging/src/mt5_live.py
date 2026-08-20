"""MT5 live-trading interfaces. Submission stays disabled unless separately authorized."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from config import Settings


class LiveTradingDisabled(RuntimeError):
    """Live MT5 submission is forbidden until separately authorized."""


@dataclass(frozen=True)
class Mt5Metadata:
    symbol: str
    broker: str | None
    account: str | None
    hedging_allowed: bool | None
    retrieved_at: datetime


@dataclass(frozen=True)
class ReconciliationReport:
    paper_open_structures: int
    broker_open_positions: int | None
    matched: bool
    notes: list[str]


@dataclass(frozen=True)
class DivergenceSample:
    observed_at: datetime
    paper_net_pips: float
    broker_net_pips: float | None
    delta_pips: float | None


def live_submission_allowed(settings: Settings) -> bool:
    return bool(settings.live_trading_authorized and settings.trading_enabled)


def kill_switch_engaged(settings: Settings) -> bool:
    return not live_submission_allowed(settings)


def idempotency_key(*, symbol: str, session: str, bar_ts: datetime, side: str) -> str:
    return f"{symbol}|{session}|{bar_ts.isoformat()}|{side}"


def fetch_metadata(settings: Settings) -> Mt5Metadata:
    return Mt5Metadata(
        symbol=settings.symbol,
        broker=None,
        account=None,
        hedging_allowed=None,
        retrieved_at=datetime.now(tz=UTC),
    )


def reconcile_paper_vs_broker(*, paper_open_structures: int) -> ReconciliationReport:
    return ReconciliationReport(
        paper_open_structures=paper_open_structures,
        broker_open_positions=None,
        matched=False,
        notes=["broker positions are unavailable while live submission is disabled"],
    )


def record_divergence(*, paper_net_pips: float) -> DivergenceSample:
    return DivergenceSample(
        observed_at=datetime.now(tz=UTC),
        paper_net_pips=paper_net_pips,
        broker_net_pips=None,
        delta_pips=None,
    )


def submit_live_order(settings: Settings, payload: dict[str, Any]) -> None:
    """Refuse live submission unless LIVE_TRADING_AUTHORIZED and TRADING_ENABLED."""
    if not live_submission_allowed(settings):
        raise LiveTradingDisabled(
            "MT5 live submission is disabled; set LIVE_TRADING_AUTHORIZED and "
            "TRADING_ENABLED only with a separate authorization. Paper does not "
            "send broker orders."
        )
    raise LiveTradingDisabled(
        "MT5 live submission remains unimplemented even when authorized; "
        f"payload keys={sorted(payload)}"
    )
