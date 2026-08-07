"""Closed-bar meta-v2 event detection, paired shadow inference, and resolution."""

from __future__ import annotations

import logging
import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import settings
from app.ml.meta.artifact import load_meta_artifact, read_active_shadow
from app.services.calendar.features import CALENDAR_MODEL_FEATURES, build_calendar_feature_frame
from app.services.calendar.store import calendar_manifest_path
from app.services.candle_quality import file_sha256
from app.services.meta_event_notifications import MetaEventNotifier
from app.services.meta_events import (
    COST_SCENARIOS,
    HORIZON,
    STOP_ATR,
    TARGET_ATR,
    _canonical_features,
    _event_id,
    _parse_tags,
)
from app.services.meta_shadow_store import MetaShadowStore
from app.services.pips import pip_size

logger = logging.getLogger(__name__)


def _load_partitions(root: Path, symbol: str, timeframe: str) -> pd.DataFrame:
    files = sorted(
        (root / f"symbol={symbol}" / f"timeframe={timeframe}").glob("year=*/month=*/part-*.parquet")
    )
    if not files:
        return pd.DataFrame()
    frame = pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    return frame.sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)


def _feature_rows(symbol: str, timeframe: str) -> pd.DataFrame:
    return _load_partitions(settings.features_dir, symbol, timeframe)


def _normalise(value: Any) -> Any:
    if value is pd.NA:
        return None
    if hasattr(value, "tolist"):
        value = value.tolist()
    elif hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and pd.isna(value):
        return None
    return value


def _candidates(features: pd.DataFrame) -> tuple[list[dict[str, Any]], Counter[str]]:
    previous_states: dict[str, str] = {}
    candidates: list[dict[str, Any]] = []
    counters: Counter[str] = Counter()
    for _, row in features.iterrows():
        tags = _parse_tags(row.get("bar_tags"))
        current_states = {str(tag.get("setup_id")): str(tag.get("state")) for tag in tags}
        eligible = []
        for tag in tags:
            setup_id = str(tag.get("setup_id", ""))
            if tag.get("state") != "complete" or tag.get("side") not in {-1, 1}:
                continue
            if tag.get("source") == "algorithm" and previous_states.get(setup_id) == "complete":
                counters["persistent_complete_suppressed"] += 1
                continue
            eligible.append(tag)
        previous_states = current_states
        by_side = {
            side: sorted(
                [tag for tag in eligible if int(tag["side"]) == side],
                key=lambda tag: (
                    -float(tag.get("confidence", 0.0)),
                    str(tag.get("setup_id")),
                ),
            )
            for side in (-1, 1)
        }
        sides = [side for side, values in by_side.items() if values]
        if len(sides) == 2:
            short_conf = float(by_side[-1][0].get("confidence", 0.0))
            long_conf = float(by_side[1][0].get("confidence", 0.0))
            if short_conf == long_conf:
                counters["opposite_side_exact_ties"] += 1
                continue
            sides = [1 if long_conf > short_conf else -1]
            counters["opposite_side_conflicts_resolved"] += 1
        for side in sides:
            candidates.append({"row": row, "side": side, "tags": by_side[side]})
    return candidates, counters


def _resolution(
    candles: pd.DataFrame,
    signal_index: int,
    side: int,
    atr: float,
    symbol: str,
) -> dict[str, Any]:
    forward = candles.iloc[signal_index + 1 : signal_index + 1 + HORIZON]
    if forward.empty:
        return {"state": "awaiting_entry"}
    entry_row = forward.iloc[0]
    entry = float(entry_row["open"])
    stop = entry - side * STOP_ATR * atr
    target = entry + side * TARGET_ATR * atr
    common = {
        "entry_ts": entry_row["ts"],
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
    }
    outcome = None
    exit_price = None
    exit_ts = None
    bars = None
    ambiguous = False
    for number, row in enumerate(forward.itertuples(index=False), start=1):
        target_hit = row.high >= target if side == 1 else row.low <= target
        stop_hit = row.low <= stop if side == 1 else row.high >= stop
        if target_hit and stop_hit:
            outcome, exit_price, ambiguous = "loss", stop, True
        elif target_hit:
            outcome, exit_price = "win", target
        elif stop_hit:
            outcome, exit_price = "loss", stop
        if outcome:
            exit_ts, bars = row.ts, number
            break
    if outcome is None and len(forward) < HORIZON:
        return {"state": "open", **common}
    if outcome is None:
        final = forward.iloc[-1]
        outcome, exit_price, exit_ts, bars = (
            "timeout",
            float(final["close"]),
            final["ts"],
            HORIZON,
        )
    risk = atr * STOP_ATR
    gross_r = side * (float(exit_price) - entry) / risk
    values: dict[str, Any] = {
        "state": "resolved",
        **common,
        "exit_ts": exit_ts,
        "exit_price": float(exit_price),
        "outcome": outcome,
        "gross_r": float(gross_r),
        "bars_to_resolution": bars,
        "ambiguous_bar": ambiguous,
        "resolved_at": datetime.now(UTC),
    }
    for pips in COST_SCENARIOS:
        suffix = int(pips)
        cost = pips * pip_size(symbol) / risk
        net = gross_r - cost
        values[f"cost_r_{suffix}"] = cost
        values[f"net_r_{suffix}"] = net
        if suffix == 3:
            values["y_meta"] = int(net > 0)
    return values


def _spread_by_ts(symbol: str, timeframe: str) -> dict[pd.Timestamp, float]:
    provenance = _load_partitions(settings.data_dir / "candle_sources", symbol, timeframe)
    if provenance.empty or "spread" not in provenance:
        return {}
    return {
        pd.Timestamp(row.ts): float(row.spread)
        for row in provenance.itertuples(index=False)
        if row.spread is not None and math.isfinite(float(row.spread))
    }


class MetaShadowWorker:
    def __init__(
        self,
        *,
        sync,
        store: MetaShadowStore,
        epic: str,
        notifier: MetaEventNotifier | None = None,
    ) -> None:
        self.sync = sync
        self.store = store
        self.epic = epic
        self.notifier = notifier or MetaEventNotifier(
            enabled=False,
            base_url="",
            api_key=None,
            channels="",
            timeout_seconds=1,
        )

    def _score(
        self, event_id: str, v1: dict[str, Any], v2: dict[str, Any]
    ) -> tuple[int, list[dict[str, Any]]]:
        pointer = read_active_shadow()
        if not pointer:
            raise RuntimeError("Meta shadow artifact pointer is unavailable")
        versions = list(
            dict.fromkeys(
                value
                for value in (pointer["active_version"], pointer.get("challenger_version"))
                if value
            )
        )
        inserted = 0
        predictions: list[dict[str, Any]] = []
        for version in versions:
            model, metadata = load_meta_artifact(version)
            feature_version = int(metadata["meta_feature_version"])
            if metadata.get("orders_enabled") is not False:
                raise RuntimeError("Shadow artifact must explicitly disable orders")
            features = v1 if feature_version == 1 else v2
            expected = tuple(metadata["feature_columns"])
            if set(features) != set(expected):
                raise RuntimeError(f"Live feature schema mismatch for {version}")
            probability = float(model.predict_proba(pd.DataFrame([features]))[0])
            threshold = float(metadata["threshold"])
            prediction = {
                "artifact_version": version,
                "event_id": event_id,
                "meta_feature_version": feature_version,
                "probability": probability,
                "threshold": threshold,
                "would_take": probability >= threshold,
            }
            persisted = self.store.insert_prediction(prediction)
            inserted += int(persisted)
            if persisted:
                predictions.append(prediction)
        return inserted, predictions

    def run_once(self) -> dict[str, Any]:
        started = datetime.now(UTC)
        notifications = Counter(
            attempted=0,
            sent=0,
            remote_skipped=0,
            failed=0,
            not_applicable=0,
        )
        try:
            synced = self.sync.sync(symbol="XAUUSD", epic=self.epic)
            if synced.unexpected_gaps:
                raise RuntimeError("Capital response contains unexpected market-open gaps")
            candles = _load_partitions(settings.data_dir / "candles", "XAUUSD", "H1")
            features = _feature_rows("XAUUSD", "H1")
            if candles.empty or features.empty:
                raise RuntimeError("Candle or feature store is unavailable")
            latest = pd.Timestamp(candles["ts"].max())
            boundary = pd.Timestamp(synced.histdata_cutoff)
            forward_start_raw = self.store.state("forward_shadow_start_ts")
            if forward_start_raw is None:
                self.store.set_state("forward_shadow_start_ts", latest.isoformat())
                forward_start = latest
            else:
                forward_start = pd.Timestamp(forward_start_raw)

            candidates, counters = _candidates(features)
            existing = self.store.event_ids()
            calendar_sha = file_sha256(calendar_manifest_path())
            inserted_events = inserted_predictions = 0
            candle_index = {value: index for index, value in enumerate(candles["ts"])}
            for candidate in candidates:
                row, side, tags = candidate["row"], candidate["side"], candidate["tags"]
                signal_ts = pd.Timestamp(row["ts"])
                if signal_ts <= boundary:
                    continue
                event_id = _event_id("XAUUSD", "H1", signal_ts, side)
                if event_id in existing:
                    continue
                primary = tags[0]
                setup_ids = sorted({str(tag["setup_id"]) for tag in tags})
                base = {
                    name: _normalise(value)
                    for name, value in _canonical_features(row, side).items()
                }
                calendar = (
                    build_calendar_feature_frame(
                        [signal_ts],
                        currencies=settings.calendar_symbol_currencies["XAUUSD"],
                    )
                    .iloc[0]
                    .to_dict()
                )
                coverage_ok = bool(calendar.pop("calendar_coverage_ok"))
                v2 = {
                    **base,
                    **{name: _normalise(calendar[name]) for name in CALENDAR_MODEL_FEATURES},
                }
                reason = None
                if not bool(row.get("data_quality_reliable", False)):
                    reason = "data_quality_unreliable"
                elif not bool(row.get("context_reliable", False)):
                    reason = "causal_context_unreliable"
                elif not coverage_ok:
                    reason = "calendar_coverage_unavailable"
                event = {
                    "event_id": event_id,
                    "symbol": "XAUUSD",
                    "timeframe": "H1",
                    "signal_ts": signal_ts,
                    "side": side,
                    "primary_setup_id": str(primary["setup_id"]),
                    "setup_ids": setup_ids,
                    "confidence": float(primary.get("confidence", 0.0)),
                    "state": "ineligible" if reason else "awaiting_entry",
                    "ineligible_reason": reason,
                    "forward_evaluation_eligible": signal_ts > forward_start,
                    "calendar_coverage_ok": coverage_ok,
                    "calendar_manifest_sha256": calendar_sha,
                    "causal_features_v1": base,
                    "causal_features_v2": v2 if coverage_ok else None,
                    "signal_close": float(row["close"]),
                    "atr_at_signal": float(row.get("atr_at_bar") or 0.0),
                    "source_boundary": boundary,
                }
                inserted = self.store.insert_event(event)
                if not inserted:
                    continue
                existing.add(event_id)
                inserted_events += 1
                if reason is None:
                    prediction_count, predictions = self._score(event_id, base, v2)
                    inserted_predictions += prediction_count
                    if event["forward_evaluation_eligible"] and predictions:
                        try:
                            result = self.notifier.notify(event, predictions)
                        except Exception as exc:
                            logger.warning(
                                "Meta-event notifier failed unexpectedly (%s); continuing",
                                type(exc).__name__,
                            )
                            notifications["attempted"] += 1
                            notifications["failed"] += 1
                        else:
                            if result.status == "disabled":
                                notifications["not_applicable"] += 1
                            else:
                                notifications["attempted"] += 1
                                notifications[result.status] += 1
                    else:
                        notifications["not_applicable"] += 1
                else:
                    notifications["not_applicable"] += 1

            spread = _spread_by_ts("XAUUSD", "H1")
            resolved = entered = 0
            for event in self.store.pending():
                signal_ts = pd.Timestamp(event["signal_ts"])
                index = candle_index.get(signal_ts)
                if index is None:
                    self.store.update_lifecycle(
                        event["event_id"],
                        {"state": "ineligible", "ineligible_reason": "signal_candle_missing"},
                    )
                    continue
                values = _resolution(
                    candles,
                    index,
                    int(event["side"]),
                    float(event["atr_at_signal"]),
                    "XAUUSD",
                )
                if values.get("entry_ts") is not None:
                    values["observed_spread"] = spread.get(pd.Timestamp(values["entry_ts"]))
                self.store.update_lifecycle(event["event_id"], values)
                entered += int(values["state"] == "open" and event["state"] == "awaiting_entry")
                resolved += int(values["state"] == "resolved")

            detail = {
                "status": "ok",
                "synced": synced.published,
                "inserted_events": inserted_events,
                "inserted_predictions": inserted_predictions,
                "entered": entered,
                "resolved": resolved,
                "latest_complete_candle": latest.isoformat(),
                "source_boundary": boundary.isoformat(),
                "forward_shadow_start_ts": forward_start.isoformat(),
                "candidate_audit": dict(counters),
                "notifications": dict(notifications),
                "orders_enabled": False,
            }
            self.store.record_run(started, "ok", detail)
            return detail
        except Exception as exc:
            self.store.record_run(
                started,
                "error",
                {
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "notifications": dict(notifications),
                    "orders_enabled": False,
                },
            )
            raise
