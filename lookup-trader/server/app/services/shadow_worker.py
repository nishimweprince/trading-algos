"""One-cycle causal shadow inference and outcome resolution."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from app.config import settings
from app.ml.outcome.infer import build_input_features, infer_outcomes
from app.services.bar_features import htf_context, tags_half
from app.services.base_rate import base_rate
from app.services.capital_sync import CapitalCandleSync
from app.services.candle_quality import unexpected_gaps
from app.services.shadow_store import ShadowStore
from app.services.pips import pip_size


def _load_candles(root: Path, symbol: str, timeframe: str) -> pd.DataFrame:
    files = sorted(
        (root / f"symbol={symbol}" / f"timeframe={timeframe}").glob(
            "year=*/month=*/part-*.parquet"
        )
    )
    if not files:
        return pd.DataFrame()
    frame = pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    return frame.sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)


def _empirical_prior(features: pd.DataFrame, side: int, cutoff: pd.Timestamp) -> dict[str, Any]:
    glob = str(settings.features_dir / "**" / "part-*.parquet")
    if not any(settings.features_dir.glob("**/part-*.parquet")):
        return {"status": "unavailable"}
    context = {
        name: features.iloc[0].get(name)
        for name in (
            "trend_state", "atr_bucket", "session", "rsi_band", "day_of_week",
            "ema_slope_bucket", "atr_change_bucket", "htf_trend_state",
            "htf_atr_bucket", "session_overlap",
        )
    }
    con = duckdb.connect(":memory:")
    try:
        escaped = glob.replace("'", "''")
        cutoff_literal = cutoff.isoformat().replace("'", "''")
        con.execute(
            "CREATE VIEW bar_features AS SELECT * FROM "
            f"read_parquet('{escaped}', hive_partitioning=1, union_by_name=1) "
            f"WHERE ts <= TIMESTAMPTZ '{cutoff_literal}'"
        )
        return base_rate(
            con, "XAUUSD", "H1", context, side=side,
            horizon=24, target_atr=1.5, stop_atr=1.0,
        )
    except Exception as exc:
        return {"status": "unavailable", "detail": type(exc).__name__}
    finally:
        con.close()


def _outcome(forward: pd.DataFrame, *, side: int, entry: float, atr: float) -> str:
    target = entry + side * 1.5 * atr
    stop = entry - side * atr
    for row in forward.iloc[:24].itertuples(index=False):
        target_hit = row.high >= target if side == 1 else row.low <= target
        stop_hit = row.low <= stop if side == 1 else row.high >= stop
        if target_hit and stop_hit:
            return "loss"  # conservative ambiguous-bar policy
        if target_hit:
            return "win"
        if stop_hit:
            return "loss"
    return "timeout"


class ShadowWorker:
    def __init__(
        self,
        *,
        sync: CapitalCandleSync,
        store: ShadowStore,
        artifact_version: str,
        epic: str,
    ) -> None:
        self.sync = sync
        self.store = store
        self.artifact_version = artifact_version
        self.epic = epic

    def run_once(self) -> dict[str, Any]:
        started = datetime.now(UTC)
        try:
            synced = self.sync.sync(symbol="XAUUSD", epic=self.epic)
            if synced.unexpected_gaps:
                raise RuntimeError("Capital.com response contains unexpected market-open gaps")
            h1 = _load_candles(settings.data_dir / "candles", "XAUUSD", "H1")
            h4 = _load_candles(settings.data_dir / "candles", "XAUUSD", "H4")
            if h1.empty or h4.empty:
                raise RuntimeError("H1/H4 candle stores are unavailable")
            boundary = pd.Timestamp(synced.histdata_cutoff)
            latest = pd.Timestamp(h1["ts"].max())
            latest_source = pd.Timestamp(synced.latest_complete_candle)
            feed_gap = pd.Timestamp(synced.capital_server_time) - latest_source
            spans_weekend = any(
                day.weekday() == 5
                for day in pd.date_range(latest_source.normalize(), pd.Timestamp(synced.capital_server_time).normalize(), freq="D")
            )
            if feed_gap > pd.Timedelta(hours=3) and not spans_weekend:
                raise RuntimeError("Capital.com feed is stale")

            existing_keys = self.store.existing_keys(artifact_version=self.artifact_version)
            anchors = h1[(h1["ts"] > boundary) & (h1["ts"] <= latest)]
            inserted = 0
            for anchor in anchors.itertuples(index=False):
                ts = pd.Timestamp(anchor.ts)
                if all((ts.isoformat(), side) in existing_keys for side in (1, -1)):
                    continue
                history = h1[h1["ts"] <= ts].tail(settings.warmup_bars)
                if len(history) < settings.ema_period:
                    continue
                htf_window = h4[h4["ts"] <= ts].tail(settings.warmup_bars)
                htf = htf_context(htf_window) if not htf_window.empty else None
                features = build_input_features(history, "XAUUSD", "H1", pip_size("XAUUSD"), htf)
                inference = infer_outcomes(
                    history, "XAUUSD", "H1", pip_size("XAUUSD"), htf,
                    artifact_version=self.artifact_version,
                )
                atr = float(features.iloc[0]["atr_at_signal"] or 0.0)
                if not math.isfinite(atr) or atr <= 0:
                    continue
                tags = tags_half(history, atr)["bar_tags"]
                spread = None
                provenance_files = sorted(
                    (settings.data_dir / "candle_sources" / "symbol=XAUUSD" / "timeframe=H1").glob(
                        "year=*/month=*/part-*.parquet"
                    )
                )
                if provenance_files:
                    provenance = pd.concat(
                        [pd.read_parquet(path) for path in provenance_files], ignore_index=True
                    )
                    provenance["ts"] = pd.to_datetime(provenance["ts"], utc=True)
                    match = provenance[provenance["ts"] == ts]
                    if not match.empty and "spread" in match:
                        spread = float(match.iloc[-1]["spread"])
                rows = []
                for direction in (inference.long, inference.short):
                    gross = 1.5 * direction.p_win - direction.p_loss
                    cost = (spread or 0.0) / atr if atr > 0 else 0.0
                    net = gross - cost
                    rows.append(
                        {
                            "artifact_version": inference.artifact_version,
                            "model_version": inference.model_version,
                            "symbol": "XAUUSD", "timeframe": "H1", "ts": ts.to_pydatetime(),
                            "side": direction.side, "direction": direction.direction,
                            "p_win": direction.p_win, "p_loss": direction.p_loss,
                            "p_timeout": direction.p_timeout, "expected_gross_r": gross,
                            "expected_net_r": net, "observed_spread": spread,
                            "action_threshold_r": 0.0, "would_trade": net > 0.0,
                            "empirical_base_rate_json": _empirical_prior(features, direction.side, boundary),
                            "tags_json": tags, "schema_sha256": inference.schema_sha256,
                            "feature_version": inference.feature_version,
                            "bar_feature_version": inference.bar_feature_version,
                            "training_source": "histdata", "live_source": "capital",
                            "source_boundary": boundary.isoformat(), "created_at": started,
                        }
                    )
                inserted += self.store.insert_predictions(rows)
                existing_keys.update((ts.isoformat(), side) for side in (1, -1))

            resolved = 0
            for pending in self.store.unresolved(artifact_version=self.artifact_version):
                ts = pd.Timestamp(pending["ts"])
                forward = h1[h1["ts"] > ts].head(24)
                if len(forward) < 24:
                    continue
                resolution_window = pd.concat(
                    [h1[h1["ts"] == ts].tail(1), forward], ignore_index=True
                )
                if unexpected_gaps(resolution_window):
                    continue
                history = h1[h1["ts"] <= ts].tail(settings.warmup_bars)
                features = build_input_features(history, "XAUUSD", "H1", pip_size("XAUUSD"))
                atr = float(features.iloc[0]["atr_at_signal"] or 0.0)
                if not math.isfinite(atr) or atr <= 0:
                    continue
                anchor = float(history.iloc[-1]["close"])
                outcome = _outcome(forward, side=int(pending["side"]), entry=anchor, atr=atr)
                resolved += int(
                    self.store.resolve(
                        artifact_version=pending["artifact_version"], symbol="XAUUSD",
                        timeframe="H1", ts=pending["ts"], side=int(pending["side"]),
                        outcome=outcome, as_of_ts=latest.to_pydatetime(),
                    )
                )
            detail = {
                "synced": synced.published, "inserted": inserted, "resolved": resolved,
                "latest_complete_candle": latest.isoformat(),
                "capital_server_time": synced.capital_server_time.isoformat(),
                "unexpected_gaps": synced.unexpected_gaps,
                "generation": synced.generation,
            }
            self.store.record_run(started, "ok", detail)
            return detail
        except Exception as exc:
            detail = {"error": type(exc).__name__, "message": str(exc)}
            self.store.record_run(started, "error", detail)
            raise
