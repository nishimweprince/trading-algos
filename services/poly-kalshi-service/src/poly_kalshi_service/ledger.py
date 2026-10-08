"""The paper cash ledger: buys debit cash, settlement **credits the payout back**.

kpc's simulator debited every buy and, at settlement, booked realised P&L
without returning the $1/$0 payout to the wallet. A $10k balance drained in
~16 h, then every order was rejected for insufficient balance and P&L
flat-lined with no alarm (kpc trap 4). Here cash is the only balance, a
settlement is a cash credit, and every reject is counted, logged and alerted.

State is persisted to ``<state_dir>/ledger.json`` after every change, so open
positions survive a restart and are settled when their resolution arrives.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
from ta_core.logging_config import log_event

__all__ = ["Ledger", "Position"]


def _day(t_ns: int) -> str:
    return datetime.fromtimestamp(t_ns / 1e9, UTC).strftime("%Y-%m-%d")


@dataclass
class Position:
    asset: str
    open_unix: int
    side: str  # "yes" | "no": the Polymarket leg held
    shares: float
    cost: float  # USDC paid for the shares, excluding the fee
    fee: float
    entry_ns: int
    status: str = "open"  # open | settled
    outcome: str | None = None
    payout: float | None = None
    pnl: float | None = None
    settled_ns: int | None = None

    @property
    def key(self) -> str:
        return f"{self.asset}:{self.open_unix}"

    @property
    def vwap(self) -> float:
        return self.cost / self.shares if self.shares else 0.0


@dataclass
class Ledger:
    start_cash: float
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    realised_by_day: dict[str, float] = field(default_factory=dict)
    rejects: int = 0
    path: Path | None = None
    epoch: str | None = None

    @classmethod
    def open(cls, path: Path | None, start_cash: float, epoch: str | None = None) -> Ledger:
        """Load the ledger; a different results epoch archives it and starts fresh."""
        if path is not None and path.is_file():
            body = json.loads(path.read_text())
            if epoch is not None and body.get("epoch") != epoch:
                archive = path.with_name(f"ledger.epoch-{body.get('epoch') or 'legacy'}.json")
                os.replace(path, archive)
                log_event("ledger_archived", archive=str(archive), new_epoch=epoch)
                return cls(start_cash=start_cash, cash=start_cash, path=path, epoch=epoch)
            ledger = cls(
                start_cash=body["start_cash"],
                cash=body["cash"],
                positions={k: Position(**v) for k, v in body["positions"].items()},
                realised_by_day=body.get("realised_by_day", {}),
                rejects=body.get("rejects", 0),
                path=path,
                epoch=body.get("epoch"),
            )
            log_event(
                "ledger_loaded",
                cash=round(ledger.cash, 2),
                open_positions=len(ledger.open_positions()),
            )
            return ledger
        return cls(start_cash=start_cash, cash=start_cash, path=path, epoch=epoch)

    def save(self) -> None:
        if self.path is None:
            return
        body = {
            "start_cash": self.start_cash,
            "cash": self.cash,
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "realised_by_day": self.realised_by_day,
            "rejects": self.rejects,
            "epoch": self.epoch,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        # orjson: this runs on the trader's loop at every buy and settlement.
        tmp.write_bytes(orjson.dumps(body, option=orjson.OPT_INDENT_2))
        os.replace(tmp, self.path)

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.status == "open"]

    def open_notional(self) -> float:
        return sum(p.cost + p.fee for p in self.open_positions())

    def realised_today(self, t_ns: int) -> float:
        return self.realised_by_day.get(_day(t_ns), 0.0)

    def can_afford(self, amount: float) -> bool:
        return amount <= self.cash + 1e-9

    def reject(self, reason: str, **fields: Any) -> None:
        self.rejects += 1
        log_event("ledger_reject", level=logging.WARNING, reason=reason, **fields)
        self.save()

    def buy(self, position: Position) -> None:
        total = position.cost + position.fee
        if position.key in self.positions:
            raise ValueError(f"second position in one window: {position.key}")
        self.cash -= total
        self.positions[position.key] = position
        self.save()

    def settle(self, asset: str, open_unix: int, outcome: str, t_ns: int) -> Position | None:
        """Credit the payout to cash. Idempotent: a settled position is left alone."""
        position = self.positions.get(f"{asset}:{open_unix}")
        if position is None or position.status != "open":
            return None
        won = position.side == outcome
        position.payout = position.shares if won else 0.0
        position.pnl = position.payout - position.cost - position.fee
        position.outcome = outcome
        position.status = "settled"
        position.settled_ns = t_ns
        self.cash += position.payout
        day = _day(t_ns)
        self.realised_by_day[day] = self.realised_by_day.get(day, 0.0) + position.pnl
        self.save()
        return position

    def snapshot(self, t_ns: int) -> dict[str, Any]:
        settled = [p for p in self.positions.values() if p.status == "settled"]
        open_ = self.open_positions()
        oldest = min((p.open_unix for p in open_), default=None)
        oldest_entry = min((p.entry_ns for p in open_), default=None)
        return {
            "cash": round(self.cash, 2),
            "start_cash": self.start_cash,
            "open_positions": len(self.open_positions()),
            "open_notional": round(self.open_notional(), 2),
            "equity_at_cost": round(self.cash + self.open_notional(), 2),
            "realised_today": round(self.realised_today(t_ns), 2),
            "realised_total": round(sum(p.pnl or 0.0 for p in settled), 2),
            "settled": len(settled),
            "rejects": self.rejects,
            "largest_open_cost": round(max((p.cost + p.fee for p in open_), default=0.0), 2),
            # Seconds the oldest unsettled position has waited since its window closed:
            # 0 while that window is still running, never negative.
            "oldest_open_age_s": None
            if oldest is None
            else max(0, round(t_ns / 1e9 - (oldest + 900))),
            "oldest_entry_age_s": None
            if oldest_entry is None
            else max(0, round((t_ns - oldest_entry) / 1e9)),
            # realised_today counts by settle day (UTC): the day cash moved.
            "realised_basis": "settle_day",
        }
