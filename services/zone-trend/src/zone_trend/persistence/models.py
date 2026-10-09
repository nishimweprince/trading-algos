"""Live state tables (the spec's schema), SQLAlchemy Core.

SQLite in WAL mode; every state change is written before the exchange call it
leads to, so after a crash the reconciler can tell what was intended from what
happened.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine

__all__ = ["METADATA", "connect"]

METADATA = MetaData()

signals = Table(
    "signals",
    METADATA,
    Column("id", String, primary_key=True),  # content hash
    Column("symbol", String),
    Column("side", String),
    Column("entry_type", String),
    Column("bar_time", Integer),
    Column("entry_ref", Float),
    Column("stop", Float),
    Column("tp1", Float),
    Column("tp2", Float),
    Column("quality", Integer),
    Column("zone_id", String),
    Column("decision", String),  # accepted | rejected
    Column("stage", String),
    Column("reject_reason", String),
    Column("mode", String),  # shadow | live
    Column("created_at", Integer),
)

orders = Table(
    "orders",
    METADATA,
    Column("client_id", String, primary_key=True),  # zt-{hash12}-{leg}
    Column("signal_id", String),
    Column("position_id", String),
    Column("symbol", String),
    Column("leg", String),  # entry | sl{n} | tp1 | tp2 | close{n}
    Column("exchange_id", String),
    Column("algo_id", String),
    Column("type", String),
    Column("side", String),
    Column("qty", Float),
    Column("price", Float),
    Column("trigger_price", Float),
    Column("reduce_only", Integer),
    Column("close_position", Integer),
    Column("status", String),  # NEW | SENT | OPEN | FILLED | CANCELED | REJECTED | UNKNOWN
    Column("last_error", Text),
    Column("created_at", Integer),
    Column("updated_at", Integer),
)

fills = Table(
    "fills",
    METADATA,
    Column("id", String, primary_key=True),  # "{symbol}:{trade id}"
    Column("client_id", String),
    Column("position_id", String),
    Column("leg", String),
    Column("price", Float),
    Column("qty", Float),
    Column("fee", Float),
    Column("fee_asset", String),
    Column("realized_pnl", Float),
    Column("maker", Integer),
    Column("ts", Integer),
)

positions = Table(
    "positions",
    METADATA,
    Column("id", String, primary_key=True),  # the signal id
    Column("symbol", String),
    Column("side", String),
    Column("signal_id", String),
    Column("entry_type", String),
    Column("entry_price", Float),
    Column("qty", Float),
    Column("qty_open", Float),
    Column("tp1_qty", Float),
    Column("single_tp", Integer),
    Column("leverage", Integer),
    Column("stop", Float),
    Column("stop_initial", Float),
    Column("stop_leg", String),  # client id of the live stop order
    Column("tp1", Float),
    Column("tp2", Float),
    Column("tp1_done", Integer),
    Column("liq_price", Float),
    Column("best", Float),
    Column("worst", Float),
    Column("mfe_pct", Float),
    Column("mae_pct", Float),
    Column("bars_open", Integer),
    Column("status", String),  # PENDING | OPEN | CLOSING | CLOSED
    Column("opened_at", Integer),
    Column("closed_at", Integer),
    Column("pnl", Float),
    Column("fees", Float),
    Column("funding", Float),
    Column("exit_reason", String),
    Column("mode", String),
)

funding_events = Table(
    "funding_events",
    METADATA,
    Column("position_id", String),
    Column("ts", Integer),
    Column("rate", Float),
    Column("amount", Float),
)

equity = Table(
    "equity",
    METADATA,
    Column("ts", Integer, primary_key=True),
    Column("balance", Float),
    Column("equity", Float),
    Column("peak", Float),
    Column("drawdown", Float),
)

displacement_stats = Table(
    "displacement_stats",
    METADATA,
    Column("symbol", String),
    Column("entry_type", String),
    Column("source", String),  # backtest | live | blended
    Column("n", Integer),
    Column("mfe_p50_r", Float),
    Column("mfe_p75_r", Float),
    Column("mae_p50_r", Float),
    Column("k_sym", Float),
    Column("updated_at", Integer),
    PrimaryKeyConstraint("symbol", "entry_type", "source"),
)

zone_snapshots = Table(
    "zone_snapshots",
    METADATA,
    Column("symbol", String),
    Column("ts", Integer),
    Column("zones_json", Text),
    PrimaryKeyConstraint("symbol", "ts"),
)

bot_state = Table(
    "bot_state",
    METADATA,
    Column("key", String, primary_key=True),  # halted, guard, last_hour, ...
    Column("value", Text),
)

events = Table(
    "events",
    METADATA,
    Column("ts", Integer),
    Column("level", String),
    Column("kind", String),
    Column("payload", Text),
)


def connect(url: str) -> Engine:
    engine = create_engine(url, future=True)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _record) -> None:  # pragma: no cover - driver hook
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=FULL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    METADATA.create_all(engine)
    return engine
