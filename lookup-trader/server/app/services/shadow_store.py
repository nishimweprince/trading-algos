"""SQLite WAL ledger isolated from manual DuckDB occurrences."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_predictions (
  artifact_version TEXT NOT NULL,
  model_version TEXT NOT NULL,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  ts TEXT NOT NULL,
  side INTEGER NOT NULL,
  direction TEXT NOT NULL,
  p_win REAL NOT NULL,
  p_loss REAL NOT NULL,
  p_timeout REAL NOT NULL,
  expected_gross_r REAL NOT NULL,
  expected_net_r REAL NOT NULL,
  observed_spread REAL,
  action_threshold_r REAL NOT NULL,
  would_trade INTEGER NOT NULL,
  empirical_base_rate_json TEXT,
  tags_json TEXT NOT NULL,
  schema_sha256 TEXT NOT NULL,
  feature_version TEXT NOT NULL,
  bar_feature_version TEXT NOT NULL,
  training_source TEXT NOT NULL,
  live_source TEXT NOT NULL,
  source_boundary TEXT NOT NULL,
  created_at TEXT NOT NULL,
  outcome TEXT,
  resolution_as_of_ts TEXT,
  resolved_at TEXT,
  PRIMARY KEY (artifact_version, symbol, timeframe, ts, side)
);
CREATE INDEX IF NOT EXISTS idx_shadow_pending
ON shadow_predictions(symbol, timeframe, ts) WHERE outcome IS NULL;

CREATE TABLE IF NOT EXISTS shadow_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  status TEXT NOT NULL,
  detail_json TEXT NOT NULL
);
"""


def _iso(value: datetime | str) -> str:
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _json_default(value: Any) -> Any:
    if hasattr(value, "item"):
        return value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class ShadowStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=10000")
        con.executescript(SCHEMA)
        return con

    def insert_predictions(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        columns = [
            "artifact_version", "model_version", "symbol", "timeframe", "ts", "side",
            "direction", "p_win", "p_loss", "p_timeout", "expected_gross_r",
            "expected_net_r", "observed_spread", "action_threshold_r", "would_trade",
            "empirical_base_rate_json", "tags_json", "schema_sha256", "feature_version",
            "bar_feature_version", "training_source", "live_source", "source_boundary",
            "created_at",
        ]
        values = []
        for row in rows:
            prepared = dict(row)
            for name in ("empirical_base_rate_json", "tags_json"):
                if not isinstance(prepared.get(name), str):
                    prepared[name] = json.dumps(
                        prepared.get(name), separators=(",", ":"), default=_json_default
                    )
            prepared["would_trade"] = int(bool(prepared["would_trade"]))
            prepared["ts"] = _iso(prepared["ts"])
            prepared["created_at"] = _iso(prepared["created_at"])
            values.append(tuple(prepared.get(name) for name in columns))
        placeholders = ",".join("?" for _ in columns)
        with self.connect() as con:
            before = con.total_changes
            con.executemany(
                f"INSERT OR IGNORE INTO shadow_predictions ({','.join(columns)}) VALUES ({placeholders})",
                values,
            )
            return con.total_changes - before

    def unresolved(self, *, artifact_version: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE outcome IS NULL"
        params: list[Any] = []
        if artifact_version:
            where += " AND artifact_version = ?"
            params.append(artifact_version)
        with self.connect() as con:
            rows = con.execute(
                f"SELECT * FROM shadow_predictions {where} ORDER BY ts, side", params
            ).fetchall()
        return [dict(row) for row in rows]

    def existing_keys(self, *, artifact_version: str) -> set[tuple[str, int]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT ts, side FROM shadow_predictions WHERE artifact_version = ?",
                [artifact_version],
            ).fetchall()
        return {(str(row["ts"]), int(row["side"])) for row in rows}

    def latest_predictions(self, *, artifact_version: str) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.connect() as con:
            row = con.execute(
                "SELECT max(ts) AS ts FROM shadow_predictions WHERE artifact_version = ?",
                [artifact_version],
            ).fetchone()
            if not row or row["ts"] is None:
                return []
            rows = con.execute(
                "SELECT * FROM shadow_predictions WHERE artifact_version = ? AND ts = ? ORDER BY side",
                [artifact_version, row["ts"]],
            ).fetchall()
        return [dict(value) for value in rows]

    def resolve(
        self,
        *,
        artifact_version: str,
        symbol: str,
        timeframe: str,
        ts: str,
        side: int,
        outcome: str,
        as_of_ts: datetime,
    ) -> bool:
        now = datetime.now(UTC).isoformat()
        with self.connect() as con:
            cursor = con.execute(
                """
                UPDATE shadow_predictions
                SET outcome = ?, resolution_as_of_ts = ?, resolved_at = ?
                WHERE artifact_version = ? AND symbol = ? AND timeframe = ?
                  AND ts = ? AND side = ? AND outcome IS NULL
                """,
                [
                    outcome, _iso(as_of_ts), now, artifact_version, symbol, timeframe, ts, side
                ],
            )
            return cursor.rowcount == 1

    def history(
        self,
        *,
        symbol: str,
        timeframe: str,
        date_from: datetime,
        date_to: datetime,
        revealed_through: datetime | None = None,
    ) -> list[dict[str, Any]]:
        cutoff = revealed_through or date_to
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT * FROM shadow_predictions
                WHERE symbol = ? AND timeframe = ? AND ts >= ? AND ts <= ?
                ORDER BY ts, side
                """,
                [symbol, timeframe, _iso(date_from), _iso(min(date_to, cutoff))],
            ).fetchall()
        result = []
        reveal_iso = _iso(cutoff)
        for raw in rows:
            row = dict(raw)
            if row["resolution_as_of_ts"] and row["resolution_as_of_ts"] > reveal_iso:
                row["outcome"] = row["resolution_as_of_ts"] = row["resolved_at"] = None
            row["would_trade"] = bool(row["would_trade"])
            for name in ("empirical_base_rate_json", "tags_json"):
                row[name.removesuffix("_json")] = (
                    json.loads(row.pop(name)) if row.get(name) else None
                )
            result.append(row)
        return result

    def record_run(self, started_at: datetime, status: str, detail: dict[str, Any]) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO shadow_runs(started_at, finished_at, status, detail_json) VALUES (?, ?, ?, ?)",
                [
                    _iso(started_at), datetime.now(UTC).isoformat(), status,
                    json.dumps(detail, separators=(",", ":"), default=str),
                ],
            )

    def status(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"status": "not_started", "predictions": 0, "unresolved": 0}
        with self.connect() as con:
            counts = con.execute(
                "SELECT count(*), count(*) FILTER (WHERE outcome IS NULL) FROM shadow_predictions"
            ).fetchone()
            run = con.execute(
                "SELECT finished_at, status, detail_json FROM shadow_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            "status": run["status"] if run else "not_started",
            "predictions": int(counts[0]),
            "unresolved": int(counts[1]),
            "last_run_at": run["finished_at"] if run else None,
            "last_run": json.loads(run["detail_json"]) if run else None,
        }
