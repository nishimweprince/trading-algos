"""SQLite journal: every signal (taken or skipped and why), every trade, strategy state and operational events."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY, logged_at TEXT, strategy TEXT, symbol TEXT, signal_bar TEXT, entry_minute TEXT,
  pattern TEXT, tf INTEGER, side INTEGER, taken INTEGER, reason TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS signals_strategy ON signals(strategy, id);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY, strategy TEXT, symbol TEXT, side INTEGER, status TEXT,
  operation_id TEXT, position_id INTEGER, signal_bar TEXT, entry_minute INTEGER, entry_utc TEXT,
  ref_bid REAL, ref_ask REAL, fill_price REAL, lots REAL, stop_dist REAL, tp_dist REAL,
  exit_request TEXT, exit_utc TEXT, exit_price REAL, exit_reason TEXT,
  r_price REAL, r_net REAL, pips REAL, gross_usd REAL, commission_usd REAL, swap_usd REAL, net_usd REAL,
  detail TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS trades_status ON trades(status);
CREATE TABLE IF NOT EXISTS strategy_state (
  strategy TEXT PRIMARY KEY, disabled INTEGER NOT NULL DEFAULT 0, reason TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, at TEXT, level TEXT, kind TEXT, message TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

OPEN_STATES = ("pending", "open", "closing")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Journal:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def _x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.db.execute(sql, args)

    # ---------------------------------------------------------------- signals
    def signal(self, strategy: str, symbol: str, signal_bar: str, entry_minute: str, pattern: str, tf: int, side: int,
               taken: bool, reason: str | None, detail: dict | None = None) -> None:
        self._x("INSERT INTO signals(logged_at,strategy,symbol,signal_bar,entry_minute,pattern,tf,side,taken,reason,"
                "detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (now_iso(), strategy, symbol, signal_bar, entry_minute, pattern, tf, side, int(taken), reason,
                 json.dumps(detail, default=str) if detail else None))

    # ----------------------------------------------------------------- trades
    def new_trade(self, **f: Any) -> int:
        f.setdefault("updated_at", now_iso())
        cols = ",".join(f)
        cur = self._x(f"INSERT INTO trades({cols}) VALUES ({','.join('?' * len(f))})", tuple(f.values()))
        return int(cur.lastrowid)

    def update_trade(self, trade_id: int, **f: Any) -> None:
        f["updated_at"] = now_iso()
        sets = ",".join(f"{k}=?" for k in f)
        self._x(f"UPDATE trades SET {sets} WHERE id=?", (*f.values(), trade_id))

    def open_trades(self) -> list[sqlite3.Row]:
        return self._x(f"SELECT * FROM trades WHERE status IN ({','.join('?' * len(OPEN_STATES))}) ORDER BY id",
                       OPEN_STATES).fetchall()

    def closed_trades(self, strategy: str | None = None) -> list[sqlite3.Row]:
        if strategy:
            return self._x("SELECT * FROM trades WHERE status='closed' AND strategy=? ORDER BY exit_utc",
                           (strategy,)).fetchall()
        return self._x("SELECT * FROM trades WHERE status='closed' ORDER BY exit_utc").fetchall()

    def trades(self, limit: int = 200) -> list[sqlite3.Row]:
        return self._x("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ------------------------------------------------------- strategy state
    def disabled(self) -> dict[str, str]:
        rows = self._x("SELECT strategy, reason FROM strategy_state WHERE disabled=1").fetchall()
        return {r["strategy"]: r["reason"] for r in rows}

    def set_disabled(self, strategy: str, disabled: bool, reason: str | None) -> None:
        self._x("INSERT INTO strategy_state(strategy,disabled,reason,updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(strategy) DO UPDATE SET disabled=excluded.disabled, reason=excluded.reason, "
                "updated_at=excluded.updated_at", (strategy, int(disabled), reason, now_iso()))

    # ----------------------------------------------------------------- events
    def event(self, level: str, kind: str, message: str, detail: dict | None = None) -> None:
        self._x("INSERT INTO events(at,level,kind,message,detail) VALUES (?,?,?,?,?)",
                (now_iso(), level, kind, message, json.dumps(detail, default=str) if detail else None))

    def events(self, limit: int = 100) -> list[sqlite3.Row]:
        return self._x("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def get(self, k: str, default: str | None = None) -> str | None:
        row = self._x("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default

    def put(self, k: str, v: str) -> None:
        self._x("INSERT INTO kv(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
