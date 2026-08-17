"""WAL-backed live meta-event and artifact-specific shadow prediction ledger."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta_live_events (
  event_id TEXT PRIMARY KEY,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  signal_ts TEXT NOT NULL,
  side INTEGER NOT NULL,
  primary_setup_id TEXT NOT NULL,
  setup_ids_json TEXT NOT NULL,
  confidence REAL NOT NULL,
  state TEXT NOT NULL,
  ineligible_reason TEXT,
  forward_evaluation_eligible INTEGER NOT NULL,
  calendar_coverage_ok INTEGER NOT NULL,
  calendar_manifest_sha256 TEXT,
  causal_features_v1_json TEXT NOT NULL,
  causal_features_v2_json TEXT,
  empirical_history_json TEXT,
  signal_close REAL NOT NULL,
  atr_at_signal REAL NOT NULL,
  entry_ts TEXT,
  entry_price REAL,
  stop_price REAL,
  target_price REAL,
  exit_ts TEXT,
  exit_price REAL,
  outcome TEXT,
  gross_r REAL,
  cost_r_3 REAL,
  cost_r_5 REAL,
  cost_r_8 REAL,
  net_r_3 REAL,
  net_r_5 REAL,
  net_r_8 REAL,
  y_meta INTEGER,
  bars_to_resolution INTEGER,
  ambiguous_bar INTEGER,
  observed_spread REAL,
  source_boundary TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  resolved_at TEXT,
  notified_at TEXT,
  notification_status TEXT,
  notification_request_id TEXT,
  notification_attempts INTEGER NOT NULL DEFAULT 0,
  notification_attempted_at TEXT,
  UNIQUE(symbol, timeframe, signal_ts, side)
);
CREATE INDEX IF NOT EXISTS idx_meta_live_state
ON meta_live_events(state, signal_ts);
CREATE TABLE IF NOT EXISTS meta_shadow_predictions (
  artifact_version TEXT NOT NULL,
  event_id TEXT NOT NULL,
  meta_feature_version INTEGER NOT NULL,
  probability REAL NOT NULL,
  threshold REAL NOT NULL,
  would_take INTEGER NOT NULL,
  role TEXT NOT NULL DEFAULT 'challenger',
  created_at TEXT NOT NULL,
  PRIMARY KEY(artifact_version, event_id),
  FOREIGN KEY(event_id) REFERENCES meta_live_events(event_id)
);
CREATE TABLE IF NOT EXISTS meta_shadow_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  status TEXT NOT NULL,
  detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta_promotion_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  evaluated_at TEXT NOT NULL,
  snapshot_sha256 TEXT,
  status TEXT NOT NULL,
  active_version TEXT,
  challenger_version TEXT,
  report_json TEXT NOT NULL
);
"""


# Columns added after the ledger was already live. `CREATE TABLE IF NOT EXISTS`
# is a no-op on an existing table, so a fresh database gets these from SCHEMA and
# an existing one needs the ALTER. Both paths must converge or the notification
# re-delivery scan silently sees no such column on the deployed ledger.
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("notified_at", "TEXT"),
    ("notification_status", "TEXT"),
    ("notification_request_id", "TEXT"),
    ("notification_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("notification_attempted_at", "TEXT"),
    ("empirical_history_json", "TEXT"),
)

_ADDED_PREDICTION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("role", "TEXT NOT NULL DEFAULT 'challenger'"),
)


# Indexes over migrated columns, created after the ALTERs rather than inside
# SCHEMA. `executescript` runs first, so a partial index naming
# `notification_status` there fails outright on a ledger that predates the
# column — which is every deployed one.
_POST_MIGRATION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_meta_live_undelivered "
    "ON meta_live_events(forward_evaluation_eligible, notification_status) "
    "WHERE notification_status IS NULL OR notification_status = 'failed'",
)


def _migrate(con: sqlite3.Connection) -> None:
    """Bring an existing ledger up to the current column set. Idempotent."""
    existing = {row["name"] for row in con.execute("PRAGMA table_info(meta_live_events)")}
    for name, decl in _ADDED_COLUMNS:
        if name not in existing:
            con.execute(f"ALTER TABLE meta_live_events ADD COLUMN {name} {decl}")
    prediction_columns = {
        row["name"] for row in con.execute("PRAGMA table_info(meta_shadow_predictions)")
    }
    for name, decl in _ADDED_PREDICTION_COLUMNS:
        if name not in prediction_columns:
            con.execute(f"ALTER TABLE meta_shadow_predictions ADD COLUMN {name} {decl}")
    for statement in _POST_MIGRATION_INDEXES:
        con.execute(statement)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    stamp = pd_timestamp(value)
    return stamp.isoformat()


def pd_timestamp(value: Any) -> datetime:
    from pandas import Timestamp

    stamp = Timestamp(value)
    if stamp.tz is None:
        stamp = stamp.tz_localize("UTC")
    else:
        stamp = stamp.tz_convert("UTC")
    return stamp.to_pydatetime()


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


class MetaShadowStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=10000")
        con.executescript(SCHEMA)
        _migrate(con)
        return con

    def state(self, key: str) -> str | None:
        with self.connect() as con:
            row = con.execute("SELECT value FROM meta_state WHERE key = ?", [key]).fetchone()
        return str(row["value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO meta_state(key,value,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                [key, value, _now()],
            )

    def event_ids(self) -> set[str]:
        with self.connect() as con:
            rows = con.execute("SELECT event_id FROM meta_live_events").fetchall()
        return {str(row["event_id"]) for row in rows}

    def insert_event(self, row: dict[str, Any]) -> bool:
        now = _now()
        prepared = {
            **row,
            "setup_ids_json": _json(row["setup_ids"]),
            "causal_features_v1_json": _json(row["causal_features_v1"]),
            "causal_features_v2_json": (
                _json(row["causal_features_v2"])
                if row.get("causal_features_v2") is not None
                else None
            ),
            "empirical_history_json": (
                _json(row["empirical_history"])
                if row.get("empirical_history") is not None
                else None
            ),
            "forward_evaluation_eligible": int(row["forward_evaluation_eligible"]),
            "calendar_coverage_ok": int(row["calendar_coverage_ok"]),
            "signal_ts": _iso(row["signal_ts"]),
            "source_boundary": _iso(row["source_boundary"]),
            "created_at": now,
            "updated_at": now,
        }
        columns = [
            "event_id",
            "symbol",
            "timeframe",
            "signal_ts",
            "side",
            "primary_setup_id",
            "setup_ids_json",
            "confidence",
            "state",
            "ineligible_reason",
            "forward_evaluation_eligible",
            "calendar_coverage_ok",
            "calendar_manifest_sha256",
            "causal_features_v1_json",
            "causal_features_v2_json",
            "empirical_history_json",
            "signal_close",
            "atr_at_signal",
            "source_boundary",
            "created_at",
            "updated_at",
        ]
        placeholders = ",".join("?" for _ in columns)
        with self.connect() as con:
            cursor = con.execute(
                f"INSERT OR IGNORE INTO meta_live_events ({','.join(columns)}) "
                f"VALUES ({placeholders})",
                [prepared.get(name) for name in columns],
            )
            return cursor.rowcount == 1

    def pending(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM meta_live_events WHERE state IN ('awaiting_entry','open') "
                "ORDER BY signal_ts"
            ).fetchall()
        return [self._decode(dict(row)) for row in rows]

    def undelivered(
        self,
        *,
        max_attempts: int,
        retry_after: str,
        not_before: str = "0001-01-01T00:00:00+00:00",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Forward events no human has successfully been told about.

        The worker notifies once, inside the branch that first persists an event,
        and three idempotency guards stop it re-entering. That is correct for
        avoiding duplicates and fatal for delivery: a five-second timeout meant
        the only output this system produces was lost permanently. These rows are
        the ones owed a retry.

        `remote_skipped` counts as delivered — the notification service made a
        deliberate decision, and retrying would argue with it.

        `retry_after` holds off events attempted recently, so a service outage
        does not burn all five attempts inside five polling cycles. It compares
        against `notification_attempted_at` rather than `updated_at`, which the
        resolution pass bumps every cycle while a trade is open and would
        otherwise starve retries for the whole 24-bar window.
        """
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM meta_live_events "
                "WHERE forward_evaluation_eligible = 1 "
                "  AND ineligible_reason IS NULL "
                "  AND (notification_status IS NULL OR notification_status = 'failed') "
                "  AND notification_attempts < ? "
                "  AND signal_ts >= ? "
                "  AND (notification_attempted_at IS NULL OR notification_attempted_at <= ?) "
                "ORDER BY signal_ts LIMIT ?",
                [int(max_attempts), not_before, retry_after, int(limit)],
            ).fetchall()
        return [self._decode(dict(row)) for row in rows]

    def expire_undelivered(self, *, older_than: str) -> int:
        """Stop retrying stale research alerts after their review horizon."""
        with self.connect() as con:
            cursor = con.execute(
                "UPDATE meta_live_events SET notification_status='expired', updated_at=? "
                "WHERE forward_evaluation_eligible=1 AND ineligible_reason IS NULL "
                "AND signal_ts < ? "
                "AND (notification_status IS NULL OR notification_status='failed')",
                [_now(), older_than],
            )
        return int(cursor.rowcount)

    def update_lifecycle(self, event_id: str, values: dict[str, Any]) -> bool:
        allowed = {
            "state",
            "entry_ts",
            "entry_price",
            "stop_price",
            "target_price",
            "exit_ts",
            "exit_price",
            "outcome",
            "gross_r",
            "cost_r_3",
            "cost_r_5",
            "cost_r_8",
            "net_r_3",
            "net_r_5",
            "net_r_8",
            "y_meta",
            "bars_to_resolution",
            "ambiguous_bar",
            "observed_spread",
            "resolved_at",
            "ineligible_reason",
            "notified_at",
            "notification_status",
            "notification_request_id",
            "notification_attempts",
            "notification_attempted_at",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unsupported lifecycle columns: {sorted(unknown)}")
        prepared = dict(values)
        timestamps = (
            "entry_ts",
            "exit_ts",
            "resolved_at",
            "notified_at",
            "notification_attempted_at",
        )
        for name in timestamps:
            if name in prepared:
                prepared[name] = _iso(prepared[name])
        for name in ("ambiguous_bar",):
            if name in prepared and prepared[name] is not None:
                prepared[name] = int(bool(prepared[name]))
        prepared["updated_at"] = _now()
        assignments = ",".join(f"{name} = ?" for name in prepared)
        with self.connect() as con:
            cursor = con.execute(
                f"UPDATE meta_live_events SET {assignments} WHERE event_id = ?",
                [*prepared.values(), event_id],
            )
            return cursor.rowcount == 1

    def predictions_for(self, event_id: str) -> list[dict[str, Any]]:
        """Stored predictions for one event, for re-notifying an undelivered alert."""
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM meta_shadow_predictions WHERE event_id=? "
                "ORDER BY artifact_version",
                [event_id],
            ).fetchall()
        return [dict(row) for row in rows]

    def insert_prediction(self, row: dict[str, Any]) -> bool:
        with self.connect() as con:
            cursor = con.execute(
                "INSERT OR IGNORE INTO meta_shadow_predictions "
                "(artifact_version,event_id,meta_feature_version,probability,threshold,"
                "would_take,role,created_at) VALUES (?,?,?,?,?,?,?,?)",
                [
                    row["artifact_version"],
                    row["event_id"],
                    row["meta_feature_version"],
                    row["probability"],
                    row["threshold"],
                    int(row["would_take"]),
                    row.get("role", "challenger"),
                    _now(),
                ],
            )
            return cursor.rowcount == 1

    def events(
        self,
        *,
        symbol: str,
        timeframe: str,
        offset: int = 0,
        limit: int = 200,
        forward_only: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        forward_clause = " AND forward_evaluation_eligible=1" if forward_only else ""
        with self.connect() as con:
            total = con.execute(
                "SELECT count(*) FROM meta_live_events WHERE symbol=? AND timeframe=?"
                + forward_clause,
                [symbol, timeframe],
            ).fetchone()[0]
            rows = con.execute(
                "SELECT * FROM meta_live_events WHERE symbol=? AND timeframe=?"
                + forward_clause
                + " ORDER BY signal_ts DESC LIMIT ? OFFSET ?",
                [symbol, timeframe, limit, offset],
            ).fetchall()
            result = []
            for raw in rows:
                event = self._decode(dict(raw))
                predictions = con.execute(
                    "SELECT * FROM meta_shadow_predictions WHERE event_id=? "
                    "ORDER BY meta_feature_version",
                    [event["event_id"]],
                ).fetchall()
                event["predictions"] = [dict(value) for value in predictions]
                result.append(event)
        return result, int(total)

    def event_by_signal(
        self, *, symbol: str, timeframe: str, signal_ts: datetime
    ) -> dict[str, Any] | None:
        with self.connect() as con:
            raw = con.execute(
                "SELECT * FROM meta_live_events WHERE symbol=? AND timeframe=? AND signal_ts=?",
                [symbol, timeframe, _iso(signal_ts)],
            ).fetchone()
            if raw is None:
                return None
            event = self._decode(dict(raw))
            predictions = con.execute(
                "SELECT * FROM meta_shadow_predictions WHERE event_id=? "
                "ORDER BY meta_feature_version",
                [event["event_id"]],
            ).fetchall()
            event["predictions"] = [dict(value) for value in predictions]
            return event

    def event_by_id(self, event_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            raw = con.execute(
                "SELECT * FROM meta_live_events WHERE event_id=?", [event_id]
            ).fetchone()
        return self._decode(dict(raw)) if raw else None

    def record_run(self, started_at: datetime, status: str, detail: dict[str, Any]) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO meta_shadow_runs(started_at,finished_at,status,detail_json) "
                "VALUES (?,?,?,?)",
                [_iso(started_at), _now(), status, _json(detail)],
            )

    def record_promotion(self, report: dict[str, Any]) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO meta_promotion_runs(evaluated_at,snapshot_sha256,status,"
                "active_version,challenger_version,report_json) VALUES (?,?,?,?,?,?)",
                [
                    _now(),
                    report.get("snapshot_sha256"),
                    report["status"],
                    report.get("active_version"),
                    report.get("challenger_version"),
                    _json(report),
                ],
            )

    def resolved_training_events(self) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM meta_live_events WHERE state='resolved' "
                "AND calendar_coverage_ok=1 ORDER BY signal_ts,side"
            ).fetchall()
        return [self._decode(dict(row)) for row in rows]

    def paired_evaluation(
        self,
        *,
        active_version: str,
        challenger_version: str,
        since: datetime,
    ) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT e.event_id,e.signal_ts,e.side,e.net_r_8,e.y_meta,
                       a.probability AS active_probability,
                       a.threshold AS active_threshold,
                       c.probability AS challenger_probability,
                       c.threshold AS challenger_threshold
                FROM meta_live_events e
                JOIN meta_shadow_predictions a
                  ON a.event_id=e.event_id AND a.artifact_version=?
                JOIN meta_shadow_predictions c
                  ON c.event_id=e.event_id AND c.artifact_version=?
                WHERE e.state='resolved' AND e.forward_evaluation_eligible=1
                  AND e.signal_ts > ?
                ORDER BY e.signal_ts,e.side
                """,
                [active_version, challenger_version, _iso(since)],
            ).fetchall()
        return [dict(row) for row in rows]

    def status(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"status": "not_started", "events": 0, "unresolved": 0}
        with self.connect() as con:
            counts = con.execute(
                "SELECT count(*), count(*) FILTER (WHERE state != 'resolved'), "
                "count(*) FILTER (WHERE forward_evaluation_eligible=1) FROM meta_live_events"
            ).fetchone()
            run = con.execute(
                "SELECT finished_at,status,detail_json FROM meta_shadow_runs "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            promotion = con.execute(
                "SELECT evaluated_at,status,report_json FROM meta_promotion_runs "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            "status": run["status"] if run else "not_started",
            "events": int(counts[0]),
            "unresolved": int(counts[1]),
            "forward_events": int(counts[2]),
            "forward_shadow_start_ts": self.state("forward_shadow_start_ts"),
            "last_run_at": run["finished_at"] if run else None,
            "last_run": json.loads(run["detail_json"]) if run else None,
            "last_promotion": json.loads(promotion["report_json"]) if promotion else None,
        }

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        row["setup_ids"] = json.loads(row.pop("setup_ids_json"))
        row["causal_features_v1"] = json.loads(row.pop("causal_features_v1_json"))
        raw_v2 = row.pop("causal_features_v2_json")
        row["causal_features_v2"] = json.loads(raw_v2) if raw_v2 else None
        raw_empirical = row.pop("empirical_history_json", None)
        row["empirical_history"] = json.loads(raw_empirical) if raw_empirical else None
        for name in (
            "forward_evaluation_eligible",
            "calendar_coverage_ok",
            "ambiguous_bar",
        ):
            if row.get(name) is not None:
                row[name] = bool(row[name])
        return row
