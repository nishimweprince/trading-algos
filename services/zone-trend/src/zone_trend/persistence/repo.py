"""Queries over the live state tables. One method per thing the runner needs."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from zone_trend.persistence import models as m
from zone_trend.persistence.models import connect
from zone_trend.types import Signal

__all__ = ["OPEN_ORDER", "OPEN_POSITION", "Repo"]

OPEN_ORDER = ("NEW", "SENT", "OPEN", "UNKNOWN")
OPEN_POSITION = ("PENDING", "OPEN", "CLOSING")


class Repo:
    def __init__(self, url_or_engine: str | Engine):
        self.engine = connect(url_or_engine) if isinstance(url_or_engine, str) else url_or_engine

    # -- generic -----------------------------------------------------------------
    def _exec(self, stmt) -> Any:
        with self.engine.begin() as conn:
            return conn.execute(stmt)

    def _rows(self, stmt) -> list[dict]:
        with self.engine.begin() as conn:
            return [dict(r._mapping) for r in conn.execute(stmt)]

    # -- signals -----------------------------------------------------------------
    def record_signal(
        self, sig: Signal, decision: str, stage: str, reason: str, mode: str, now: int
    ) -> None:
        row = {
            "id": sig.id,
            "symbol": sig.symbol,
            "side": sig.side.value,
            "entry_type": sig.entry_type,
            "bar_time": sig.bar_time,
            "entry_ref": sig.entry_ref,
            "stop": sig.stop,
            "tp1": sig.tp1,
            "tp2": sig.tp2,
            "quality": sig.quality,
            "zone_id": sig.zone_id,
            "decision": decision,
            "stage": stage,
            "reject_reason": reason,
            "mode": mode,
            "created_at": now,
        }
        try:
            self._exec(insert(m.signals).values(**row))
        except IntegrityError:
            pass  # replayed hour: the first decision stands

    def signals_between(self, start: int, end: int) -> list[dict]:
        t = m.signals
        return self._rows(select(t).where(t.c.bar_time >= start, t.c.bar_time < end))

    # -- orders ------------------------------------------------------------------
    def insert_order(self, **row: Any) -> None:
        self._exec(insert(m.orders).values(**row))

    def update_order(self, client_id: str, **fields: Any) -> None:
        self._exec(update(m.orders).where(m.orders.c.client_id == client_id).values(**fields))

    def order(self, client_id: str) -> dict | None:
        rows = self._rows(select(m.orders).where(m.orders.c.client_id == client_id))
        return rows[0] if rows else None

    def order_by_exchange_id(self, symbol: str, exchange_id: str) -> dict | None:
        t = m.orders
        rows = self._rows(select(t).where(t.c.symbol == symbol, t.c.exchange_id == exchange_id))
        return rows[0] if rows else None

    def orders_for(self, position_id: str, open_only: bool = False) -> list[dict]:
        t = m.orders
        stmt = select(t).where(t.c.position_id == position_id)
        if open_only:
            stmt = stmt.where(t.c.status.in_(OPEN_ORDER))
        return self._rows(stmt)

    def open_orders(self) -> list[dict]:
        return self._rows(select(m.orders).where(m.orders.c.status.in_(OPEN_ORDER)))

    # -- fills -------------------------------------------------------------------
    def insert_fill(self, **row: Any) -> bool:
        """False when the fill was already recorded (stream and REST overlap)."""
        try:
            self._exec(insert(m.fills).values(**row))
            return True
        except IntegrityError:
            return False

    def fills_for(self, position_id: str) -> list[dict]:
        return self._rows(select(m.fills).where(m.fills.c.position_id == position_id))

    # -- positions -----------------------------------------------------------------
    def insert_position(self, **row: Any) -> None:
        self._exec(insert(m.positions).values(**row))

    def update_position(self, position_id: str, **fields: Any) -> None:
        t = m.positions
        self._exec(update(t).where(t.c.id == position_id).values(**fields))

    def position(self, position_id: str) -> dict | None:
        rows = self._rows(select(m.positions).where(m.positions.c.id == position_id))
        return rows[0] if rows else None

    def open_positions(self) -> list[dict]:
        t = m.positions
        return self._rows(select(t).where(t.c.status.in_(OPEN_POSITION)))

    def open_position_for(self, symbol: str) -> dict | None:
        t = m.positions
        rows = self._rows(select(t).where(t.c.symbol == symbol, t.c.status.in_(OPEN_POSITION)))
        return rows[0] if rows else None

    def closed_positions(self) -> list[dict]:
        t = m.positions
        return self._rows(select(t).where(t.c.status == "CLOSED").order_by(t.c.closed_at))

    def realized_net(self) -> float:
        t = m.positions
        with self.engine.begin() as conn:
            total = conn.execute(
                select(func.coalesce(func.sum(t.c.pnl), 0.0)).where(t.c.status == "CLOSED")
            ).scalar_one()
        return float(total)

    # -- funding, equity, state, events -------------------------------------------------
    def add_funding(self, position_id: str, ts: int, rate: float, amount: float) -> None:
        self._exec(
            insert(m.funding_events).values(
                position_id=position_id, ts=ts, rate=rate, amount=amount
            )
        )

    def record_equity(self, ts: int, balance: float, equity: float, peak: float) -> None:
        dd = 1 - equity / peak if peak > 0 else 0.0
        try:
            self._exec(
                insert(m.equity).values(
                    ts=ts, balance=balance, equity=equity, peak=peak, drawdown=dd
                )
            )
        except IntegrityError:
            pass

    def get_state(self, key: str, default: Any = None) -> Any:
        rows = self._rows(select(m.bot_state).where(m.bot_state.c.key == key))
        return json.loads(rows[0]["value"]) if rows else default

    def set_state(self, key: str, value: Any) -> None:
        text = json.dumps(value, default=str)
        with self.engine.begin() as conn:
            done = conn.execute(
                update(m.bot_state).where(m.bot_state.c.key == key).values(value=text)
            ).rowcount
            if not done:
                conn.execute(insert(m.bot_state).values(key=key, value=text))

    def log_event(self, ts: int, level: str, kind: str, payload: Any = None) -> None:
        self._exec(
            insert(m.events).values(
                ts=ts, level=level, kind=kind, payload=json.dumps(payload, default=str)
            )
        )

    def snapshot_zones(self, symbol: str, ts: int, zones: list[dict]) -> None:
        try:
            self._exec(
                insert(m.zone_snapshots).values(symbol=symbol, ts=ts, zones_json=json.dumps(zones))
            )
        except IntegrityError:
            pass
