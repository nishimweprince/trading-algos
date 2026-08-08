"""Automatic, immutable meta-events derived from causal tags and future candles."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from app.config import settings
from app.db.duck import register_candles_view
from app.db.setups_seed import SEED_SETUPS
from app.services.candle_audit import exclusion_path, is_reliable, load_exclusions
from app.services.candle_quality import file_sha256
from app.services.pips import pip_size

META_EVENT_NAMESPACE = uuid.UUID("4cb7e20b-d9eb-4eab-a9a0-ff0d519e47dd")
META_FEATURE_VERSION_V1 = 1
META_EVENT_MANIFEST_VERSION_V1 = 1
HORIZON = 24
# A 1 ATR stop is roughly one average hourly range, which on a 24-bar hold was
# being taken out by noise rather than by the setup failing: it lost 60.4% of
# events against 51.3% at 2 ATR, and gross expectancy was negative (-0.020R)
# where at 2 ATR it is not (+0.002R). Widening also halves cost per unit of risk,
# because risk is denominated in the stop. Measured over all 25,332 events, net R
# improves from -0.111 to -0.044 per event.
#
# 2.0 was chosen ahead of the sweep rather than read off it. The grid is monotone
# in both stop and reward — every widening looks better — but that is cost
# dilution plus timeouts absorbing losses, not an edge: at a 3 ATR stop 40.8% of
# events never touch a barrier at all. 2 ATR takes most of the improvement while
# the contract remains a triple barrier.
STOP_ATR = 2.0
TARGET_ATR = 3.0
REWARD_R = TARGET_ATR / STOP_ATR
"""Reward per unit of risk. Every `_r` column below is in R — multiples of the
stop distance — not in ATR. The two coincided only while `STOP_ATR` was 1.0; with
a wider stop, dividing by ATR would report a loss as -2.0 and make expectancy
incomparable across contracts. `y_meta` is unaffected either way, since the two
scales differ by the positive constant `STOP_ATR` and so share a sign."""
COST_SCENARIOS = (3.0, 5.0, 8.0)
SETUP_CATEGORY = {row[0]: row[3] for row in SEED_SETUPS}

# Explicit causal model boundary. Price levels, absolute ATR, volume and every
# fwd/label field stay outside this list even though some remain in the export as
# operational auxiliaries.
META_MODEL_FEATURES = (
    "trend_state",
    "atr_bucket",
    "session",
    "rsi_band",
    "day_of_week",
    "ema_slope_bucket",
    "atr_change_bucket",
    "htf_trend_state",
    "dist_day_high_atr",
    "dist_day_low_atr",
    "gap_atr",
    "signal_body_pct",
    "signal_upper_wick_pct",
    "signal_lower_wick_pct",
    "signal_range_atr",
    "bars_since_swing_high",
    "bars_since_swing_low",
    "rsi_value",
    "atr_pct",
    "dist_ema_atr",
    "ema_slope_atr",
    "atr_change_ratio",
    "warmup_bars_available",
    "back_bars_available",
    "efficiency_ratio",
    "close_range_pct",
    "realized_vol_atr",
    "round_number_dist_atr",
    "htf_atr_bucket",
    "htf_range_pct",
    "dist_swing_high_atr",
    "dist_swing_low_atr",
    "prior_day_high_dist_atr",
    "prior_day_low_dist_atr",
    "prior_week_high_dist_atr",
    "prior_week_low_dist_atr",
    "bar_in_session",
    "session_overlap",
    "shape_48",
)


def event_path() -> Path:
    return settings.data_dir / "exports" / "meta_events_v1.parquet"


def event_manifest_path() -> Path:
    return settings.data_dir / "exports" / "meta_events_v1.manifest.json"


def event_report_path(symbol: str, timeframe: str) -> Path:
    return settings.data_dir / "reports" / f"meta-events-{symbol}-{timeframe}-v1.json"


def _parse_tags(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        return []
    return [item for item in raw.get("tags", []) if isinstance(item, dict)]


def _shape_for_side(value: Any, side: int) -> Any:
    if side == 1 or value is None or (isinstance(value, float) and np.isnan(value)):
        return value
    values = list(value)
    reflected: list[float] = []
    for index in range(0, len(values), 4):
        chunk = values[index : index + 4]
        if len(chunk) != 4:
            reflected.extend(-float(item) for item in chunk)
        else:
            opn, high, low, close = (float(item) for item in chunk)
            reflected.extend([-opn, -low, -high, -close])
    return reflected


def _relative_trend(value: Any, side: int) -> Any:
    if value not in {"up", "down"}:
        return value
    aligned = (value == "up" and side == 1) or (value == "down" and side == -1)
    return "aligned" if aligned else "opposed"


def _canonical_features(row: pd.Series, side: int) -> dict[str, Any]:
    out = {name: row.get(name) for name in META_MODEL_FEATURES}
    out["trend_state"] = _relative_trend(out.get("trend_state"), side)
    out["htf_trend_state"] = _relative_trend(out.get("htf_trend_state"), side)
    for name in ("dist_ema_atr", "ema_slope_atr", "gap_atr"):
        value = out.get(name)
        if value is not None and not pd.isna(value):
            out[name] = float(value) * side
    for high_name, low_name in (
        ("dist_day_high_atr", "dist_day_low_atr"),
        ("dist_swing_high_atr", "dist_swing_low_atr"),
        ("prior_day_high_dist_atr", "prior_day_low_dist_atr"),
        ("prior_week_high_dist_atr", "prior_week_low_dist_atr"),
    ):
        if side == -1:
            out[high_name], out[low_name] = out.get(low_name), out.get(high_name)
    if side == -1:
        out["signal_upper_wick_pct"], out["signal_lower_wick_pct"] = (
            out.get("signal_lower_wick_pct"),
            out.get("signal_upper_wick_pct"),
        )
        position = out.get("close_range_pct")
        if position is not None and not pd.isna(position):
            out["close_range_pct"] = 1.0 - float(position)
    out["shape_48"] = _shape_for_side(out.get("shape_48"), side)
    return out


def _event_id(symbol: str, timeframe: str, signal_ts: Any, side: int) -> str:
    key = (
        f"meta-label-v{settings.meta_label_version}|{symbol}|{timeframe}|"
        f"{pd.Timestamp(signal_ts).isoformat()}|{side}"
    )
    return str(uuid.uuid5(META_EVENT_NAMESPACE, key))


def indicative_price_levels(reference_price: float, atr: float, side: int) -> dict[str, float]:
    """Return causal absolute levels anchored to a price already known at signal time.

    The production contract enters at the next H1 open, which is deliberately
    unavailable when a signal is first scored.  Replay and alert surfaces use
    the signal close as an explicitly indicative anchor; the same directional
    arithmetic is reapplied to the actual next open when the event enters.
    """
    reference = float(reference_price)
    atr_value = float(atr)
    if side not in {-1, 1}:
        raise ValueError("side must be -1 or 1")
    if not np.isfinite(reference) or not np.isfinite(atr_value) or atr_value <= 0:
        raise ValueError("reference price and ATR must be finite, with ATR greater than zero")
    return {
        "reference_price": reference,
        "atr_at_signal": atr_value,
        "stop_price": reference - side * STOP_ATR * atr_value,
        "target_price": reference + side * TARGET_ATR * atr_value,
    }


def _resolve_label(
    candles: pd.DataFrame, signal_index: int, side: int, atr: float, symbol: str
) -> dict[str, Any] | None:
    forward = candles.iloc[signal_index + 1 : signal_index + 1 + HORIZON]
    if len(forward) < HORIZON or not atr or pd.isna(atr):
        return None
    entry_bar = forward.iloc[0]
    entry = float(entry_bar["open"])
    levels = indicative_price_levels(entry, atr, side)
    target = levels["target_price"]
    stop = levels["stop_price"]
    outcome, gross_r, resolution_bar, exit_price, ambiguous = "timeout", None, HORIZON, None, False
    for number, bar in enumerate(forward.itertuples(index=False), start=1):
        target_hit = float(bar.high) >= target if side == 1 else float(bar.low) <= target
        stop_hit = float(bar.low) <= stop if side == 1 else float(bar.high) >= stop
        if target_hit and stop_hit:
            outcome, gross_r = "loss", -1.0
            resolution_bar, exit_price, ambiguous = number, stop, True
            break
        if stop_hit:
            outcome, gross_r, resolution_bar, exit_price = "loss", -1.0, number, stop
            break
        if target_hit:
            outcome, gross_r, resolution_bar, exit_price = "win", REWARD_R, number, target
            break
    if gross_r is None:
        final = forward.iloc[-1]
        exit_price = float(final["close"])
        gross_r = (exit_price - entry) * side / (atr * STOP_ATR)
    exit_row = forward.iloc[resolution_bar - 1]
    result: dict[str, Any] = {
        "entry_ts": pd.Timestamp(entry_bar["ts"]),
        "exit_ts": pd.Timestamp(exit_row["ts"]),
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "exit_price": exit_price,
        "outcome": outcome,
        "gross_r": float(gross_r),
        "bars_to_resolution": resolution_bar,
        "ambiguous_bar": ambiguous,
    }
    pip = pip_size(symbol)
    for cost in COST_SCENARIOS:
        suffix = str(int(cost))
        # Per unit of risk, so a wider stop genuinely dilutes the spread rather
        # than only appearing to. Same denominator as `gross_r` above.
        cost_r = cost * pip / (atr * STOP_ATR)
        net_r = float(gross_r) - cost_r
        result[f"cost_r_{suffix}"] = cost_r
        result[f"net_r_{suffix}"] = net_r
        result[f"y_meta_{suffix}"] = int(net_r > 0)
    result["y_meta"] = result["y_meta_3"]
    return result


def _feature_profile(frame: pd.DataFrame) -> dict[str, Any]:
    profile: dict[str, Any] = {}
    for name in META_MODEL_FEATURES:
        if name not in frame:
            continue
        series = frame[name]
        non_null = int(series.notna().sum())
        if name == "shape_48":
            unique = None
            all_zero = False
        else:
            unique = int(series.nunique(dropna=True))
            numeric = pd.to_numeric(series, errors="coerce")
            all_zero = bool(non_null and numeric.notna().sum() == non_null and (numeric == 0).all())
        numeric_values = pd.to_numeric(series, errors="coerce") if name != "shape_48" else None
        non_zero_rate = (
            float((numeric_values.fillna(0) != 0).mean()) if numeric_values is not None else None
        )
        profile[name] = {
            "non_null": non_null,
            "non_null_rate": non_null / len(frame) if len(frame) else 0.0,
            "unique": unique,
            "constant": unique is not None and unique <= 1,
            "all_zero": all_zero,
            "non_zero_rate": non_zero_rate,
            "sparse_below": [
                threshold
                for threshold in (0.001, 0.005, 0.01)
                if non_zero_rate is not None and non_zero_rate < threshold
            ],
        }
    return profile


def _schema_sha256(frame: pd.DataFrame) -> str:
    schema = [{"name": name, "dtype": str(frame[name].dtype)} for name in frame.columns]
    return hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()


def export_meta_events(symbol: str, timeframe: str, path: Path | None = None) -> int:
    symbol, timeframe = symbol.upper(), timeframe.upper()
    path = path or event_path()
    feature_root = settings.features_dir / f"symbol={symbol}" / f"timeframe={timeframe}"
    feature_glob = str(feature_root / "**" / "part-*.parquet")
    if not list(feature_root.glob("**/part-*.parquet")):
        raise ValueError(f"No feature store found for {symbol} {timeframe}")
    con = duckdb.connect(":memory:")
    try:
        register_candles_view(con, force=True)
        candles = con.execute(
            "SELECT ts, open, high, low, close, volume FROM candles "
            "WHERE symbol = ? AND timeframe = ? ORDER BY ts",
            [symbol, timeframe],
        ).fetchdf()
        features = con.execute(
            f"SELECT * FROM read_parquet('{feature_glob}', "
            "hive_partitioning=true, union_by_name=true) "
            "WHERE bar_feature_version = ? ORDER BY ts",
            [settings.bar_feature_version],
        ).fetchdf()
    finally:
        con.close()
    candles["ts"] = pd.to_datetime(candles["ts"], utc=True)
    features["ts"] = pd.to_datetime(features["ts"], utc=True)
    candle_index = {value: index for index, value in enumerate(candles["ts"])}
    intervals = load_exclusions(symbol, timeframe)
    counters: Counter[str] = Counter()
    candidates: list[dict[str, Any]] = []
    previous_states: dict[str, str] = {}

    for _, row in features.iterrows():
        signal_ts = row["ts"]
        reliable = bool(row.get("data_quality_reliable", is_reliable(signal_ts, intervals)))
        context_reliable = bool(row.get("context_reliable", False))
        tags = _parse_tags(row.get("bar_tags"))
        current_states = {str(tag.get("setup_id")): str(tag.get("state")) for tag in tags}
        eligible: list[dict[str, Any]] = []
        for tag in tags:
            setup_id = str(tag.get("setup_id", ""))
            if tag.get("state") != "complete" or tag.get("side") not in {-1, 1}:
                continue
            if tag.get("source") == "algorithm" and previous_states.get(setup_id) == "complete":
                counters["persistent_complete_suppressed"] += 1
                continue
            eligible.append(tag)
        # Absence is a reset. Keeping stale states here would suppress the next
        # genuinely new completion forever after the first occurrence.
        previous_states = current_states
        if not reliable:
            counters["unreliable_data"] += len(eligible)
            continue
        if not context_reliable:
            counters["unreliable_context"] += len(eligible)
            continue
        by_side = {
            side: sorted(
                [tag for tag in eligible if int(tag["side"]) == side],
                key=lambda tag: (-float(tag.get("confidence", 0.0)), str(tag.get("setup_id"))),
            )
            for side in (-1, 1)
        }
        selected_sides = [side for side, items in by_side.items() if items]
        if len(selected_sides) == 2:
            short_conf = float(by_side[-1][0].get("confidence", 0.0))
            long_conf = float(by_side[1][0].get("confidence", 0.0))
            if short_conf == long_conf:
                counters["opposite_side_exact_ties"] += 1
                continue
            selected_sides = [1 if long_conf > short_conf else -1]
            counters["opposite_side_conflicts_resolved"] += 1
        for side in selected_sides:
            items = by_side[side]
            candidates.append({"row": row, "side": side, "tags": items})

    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        row, side, tags = candidate["row"], candidate["side"], candidate["tags"]
        signal_ts = row["ts"]
        index = candle_index.get(signal_ts)
        if index is None:
            counters["signal_candle_missing"] += 1
            continue
        label_window = candles.iloc[index : index + HORIZON + 1]
        if any(not is_reliable(value, intervals) for value in label_window["ts"]):
            counters["label_window_excluded"] += 1
            continue
        atr = float(row.get("atr_at_bar") or row.get("atr_at_signal") or 0.0)
        label = _resolve_label(candles, index, side, atr, symbol)
        if label is None:
            counters["incomplete_future_or_atr"] += 1
            continue
        primary = tags[0]
        setup_ids = sorted({str(tag["setup_id"]) for tag in tags})
        output: dict[str, Any] = {
            "event_id": _event_id(symbol, timeframe, signal_ts, side),
            "symbol": symbol,
            "timeframe": timeframe,
            "signal_ts": signal_ts,
            "side": side,
            "primary_setup_id": str(primary["setup_id"]),
            "setup_ids": json.dumps(setup_ids, separators=(",", ":")),
            "confidence": float(primary.get("confidence", 0.0)),
            "tag_source": str(primary.get("source", "")),
            "tagger_model_version": primary.get("model_version"),
            "data_quality_reliable": True,
            "context_reliable": True,
            "bar_feature_version": settings.bar_feature_version,
            "meta_feature_version": META_FEATURE_VERSION_V1,
            "meta_label_version": settings.meta_label_version,
            "signal_close": float(row["close"]),
            "atr_at_signal": atr,
            **_canonical_features(row, side),
            **label,
        }
        rows.append(output)

    output = (
        pd.DataFrame(rows).sort_values(
            ["signal_ts", "side", "event_id"], kind="stable", ignore_index=True
        )
        if rows
        else pd.DataFrame()
    )
    if output.empty:
        raise ValueError("No eligible automated meta-events were generated")
    path.parent.mkdir(parents=True, exist_ok=True)
    output.to_parquet(path, index=False)
    persisted = pd.read_parquet(path)
    exclusion_file = exclusion_path(symbol, timeframe)
    counts = {
        "by_year": persisted.assign(year=pd.to_datetime(persisted["signal_ts"]).dt.year)
        .groupby("year")
        .size()
        .astype(int)
        .to_dict(),
        "by_setup": persisted.groupby("primary_setup_id").size().astype(int).to_dict(),
        "by_side": {
            str(key): int(value) for key, value in persisted.groupby("side").size().items()
        },
        "by_outcome": persisted.groupby("outcome").size().astype(int).to_dict(),
    }
    setup_profile = {
        setup_id: {
            "count": int(count),
            "rate": int(count) / len(persisted),
            "sparse_below": [
                threshold
                for threshold in (0.001, 0.005, 0.01)
                if int(count) / len(persisted) < threshold
            ],
        }
        for setup_id, count in counts["by_setup"].items()
    }
    candle_source_root = (
        settings.data_dir / "candles" / f"symbol={symbol}" / f"timeframe={timeframe}"
    )
    manifest = {
        "manifest_version": META_EVENT_MANIFEST_VERSION_V1,
        "dataset_contract": f"{symbol}-{timeframe}-meta-events-v1",
        "rows": len(persisted),
        "schema_sha256": _schema_sha256(persisted),
        "parquet_sha256": file_sha256(path),
        "columns": list(persisted.columns),
        "source": {
            "kind": "hive_partitioned_candles_and_bar_features",
            "candle_files": [
                {
                    "path": str(source.relative_to(settings.data_dir)),
                    "bytes": source.stat().st_size,
                    "sha256": file_sha256(source),
                }
                for source in sorted(candle_source_root.glob("year=*/month=*/part-*.parquet"))
            ],
        },
        "model_feature_columns": list(META_MODEL_FEATURES),
        "auxiliary_columns": [
            "signal_close",
            "atr_at_signal",
            "entry_price",
            "stop_price",
            "target_price",
            "exit_price",
            "gross_r",
            "cost_r_3",
            "cost_r_5",
            "cost_r_8",
            "net_r_3",
            "net_r_5",
            "net_r_8",
            "outcome",
            "bars_to_resolution",
            "ambiguous_bar",
            "y_meta",
            "y_meta_3",
            "y_meta_5",
            "y_meta_8",
        ],
        "versions": {
            "bar_feature": settings.bar_feature_version,
            "meta_feature": META_FEATURE_VERSION_V1,
            "meta_label": settings.meta_label_version,
        },
        "label_policy": {
            "entry": "next_h1_open",
            "horizon_bars": HORIZON,
            "target_atr": TARGET_ATR,
            "stop_atr": STOP_ATR,
            "reward_r": REWARD_R,
            "r_unit": "stop_distance",
            "same_bar_policy": "loss",
            "cost_scenarios_pips": list(COST_SCENARIOS),
            "primary_label": "net_r_3 > 0",
        },
        "exclusion_manifest": {
            "path": (
                str(exclusion_file.relative_to(settings.data_dir))
                if exclusion_file.exists()
                else None
            ),
            "sha256": file_sha256(exclusion_file) if exclusion_file.exists() else None,
        },
        "counts": counts,
        "drops": dict(sorted(counters.items())),
        "feature_profile": _feature_profile(persisted),
        "setup_profile": setup_profile,
    }
    event_manifest_path().write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    report = {
        "report_version": 1,
        "status": "ready_for_review",
        "symbol": symbol,
        "timeframe": timeframe,
        "rows": len(persisted),
        "range": {
            "min": pd.Timestamp(persisted["signal_ts"].min()).isoformat(),
            "max": pd.Timestamp(persisted["signal_ts"].max()).isoformat(),
        },
        "counts": counts,
        "drops": dict(sorted(counters.items())),
        "training_inclusion_mutable_by_review": False,
    }
    report_file = event_report_path(symbol, timeframe)
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return len(persisted)
