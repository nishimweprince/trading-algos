"""Advisory MFE watcher: notify once when a filled trade reaches the break-even trigger.

This tracker is **advisory only**. It never moves a stop — mt5-trader exposes no
position-modification endpoint, so the notification tells the operator to move the
stop themselves.

Two limitations follow from that and are deliberate, not oversights:

* Excursion is sampled at ``POLL_INTERVAL_SECONDS``. A spike that touches the
  trigger and retraces inside one interval is missed.
* Without a positions endpoint the tracker cannot observe a real close, so it
  *infers* one when price has travelled the take-profit distance in favour or the
  stop-loss distance against, or when the trade exceeds its TTL. An inferred close
  is a guess about the broker's state, never a fact.

State is persisted so a restart keeps watching trades that are still open.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .data_client import Tick


@dataclass(slots=True)
class TrackedTrade:
    signal_id: str
    quote: str
    symbol: str
    direction: str  # "buy" | "sell"
    entry: float
    pip_size: float
    stop_loss_pips: float
    take_profit_pips: float
    opened_at: str
    mfe_pips: float = 0.0
    mae_pips: float = 0.0
    break_even_notified: bool = False

    def excursion_pips(self, tick: Tick) -> float:
        """Signed excursion in pips, using the price the position would close at.

        Rounded to a ten-thousandth of a pip: dividing a price difference by the pip
        size lands just under the true value in binary floating point
        (``(1.1030 - 1.1000) / 0.0001`` is ``29.999999999998916``), which would stop a
        trade sitting exactly on the trigger from ever reaching it.
        """
        if self.direction == "buy":
            raw = (tick.bid - self.entry) / self.pip_size
        else:
            raw = (self.entry - tick.ask) / self.pip_size
        return round(raw, 4)

    def age(self, now: datetime) -> timedelta:
        opened = datetime.fromisoformat(self.opened_at)
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=UTC)
        return now - opened


@dataclass(slots=True)
class TrackerUpdate:
    """What a single price sample implies for one tracked trade."""

    trade: TrackedTrade
    break_even_reached: bool = False
    closed_reason: str | None = None


class PositionTracker:
    """In-memory set of open trades, persisted to ``state_path`` after every change."""

    def __init__(
        self,
        state_path: Path,
        break_even_pips: float,
        ttl_hours: float,
    ) -> None:
        self.state_path = state_path
        self.break_even_pips = break_even_pips
        self.ttl = timedelta(hours=ttl_hours)
        self._trades: dict[str, TrackedTrade] = {}

    # -- lifecycle ---------------------------------------------------------

    def load(self) -> None:
        if not self.state_path.is_file():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, list):
            return
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                trade = TrackedTrade(**item)
            except TypeError:
                continue
            self._trades[trade.signal_id] = trade

    def save(self) -> None:
        """Atomic write: a crash mid-write must not leave a truncated state file."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(f"{self.state_path.suffix}.tmp")
        tmp.write_text(
            json.dumps([asdict(t) for t in self._trades.values()], indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.state_path)

    # -- accessors ---------------------------------------------------------

    @property
    def trades(self) -> list[TrackedTrade]:
        return list(self._trades.values())

    def quotes(self) -> list[str]:
        """Distinct quotes needing a price sample this tick."""
        return sorted({trade.quote for trade in self._trades.values()})

    def trades_for_quote(self, quote: str) -> list[TrackedTrade]:
        return [trade for trade in self._trades.values() if trade.quote == quote]

    # -- mutation ----------------------------------------------------------

    def track(self, trade: TrackedTrade) -> bool:
        """Register a filled trade. Returns False when already tracked."""
        if trade.signal_id in self._trades:
            return False
        self._trades[trade.signal_id] = trade
        self.save()
        return True

    def observe(self, quote: str, tick: Tick, now: datetime | None = None) -> list[TrackerUpdate]:
        """Apply one price sample to every trade on ``quote``.

        Returns an update per trade whose state changed in a way worth reporting:
        the break-even trigger being reached, or an inferred close.
        """
        moment = now or datetime.now(UTC)
        updates: list[TrackerUpdate] = []

        for trade in self.trades_for_quote(quote):
            excursion = trade.excursion_pips(tick)
            trade.mfe_pips = max(trade.mfe_pips, excursion)
            trade.mae_pips = min(trade.mae_pips, excursion)

            update = TrackerUpdate(trade=trade)
            if not trade.break_even_notified and trade.mfe_pips >= self.break_even_pips:
                trade.break_even_notified = True
                update.break_even_reached = True

            update.closed_reason = self._closed_reason(trade, moment)
            if update.closed_reason is not None:
                del self._trades[trade.signal_id]

            if update.break_even_reached or update.closed_reason is not None:
                updates.append(update)

        if updates:
            self.save()
        return updates

    def expire(self, now: datetime | None = None) -> list[TrackerUpdate]:
        """Drop trades past their TTL even when no price sample arrived."""
        moment = now or datetime.now(UTC)
        updates: list[TrackerUpdate] = []
        for trade in self.trades:
            if trade.age(moment) >= self.ttl:
                del self._trades[trade.signal_id]
                updates.append(TrackerUpdate(trade=trade, closed_reason="ttl_expired"))
        if updates:
            self.save()
        return updates

    def _closed_reason(self, trade: TrackedTrade, now: datetime) -> str | None:
        if trade.mfe_pips >= trade.take_profit_pips:
            return "take_profit_reached"
        if trade.mae_pips <= -trade.stop_loss_pips:
            return "stop_loss_reached"
        if trade.age(now) >= self.ttl:
            return "ttl_expired"
        return None


def tracked_trade_from_fill(
    *,
    signal_id: str,
    quote: str,
    symbol: str,
    direction: str,
    fallback_entry: float,
    pip_size: float,
    stop_loss_pips: float,
    take_profit_pips: float,
    detail: dict[str, Any] | None,
    opened_at: datetime | None = None,
) -> TrackedTrade:
    """Build a tracked trade from an mt5-trader submit response.

    Prefers the broker's ``execution_price`` over the strategy's bar close: the fill
    is what break-even actually has to beat.
    """
    entry = fallback_entry
    if detail is not None:
        raw = detail.get("execution_price")
        if raw is not None:
            try:
                entry = float(raw)
            except (TypeError, ValueError):
                entry = fallback_entry

    return TrackedTrade(
        signal_id=signal_id,
        quote=quote,
        symbol=symbol,
        direction=direction,
        entry=entry,
        pip_size=pip_size,
        stop_loss_pips=stop_loss_pips,
        take_profit_pips=take_profit_pips,
        opened_at=(opened_at or datetime.now(UTC)).isoformat(),
    )
