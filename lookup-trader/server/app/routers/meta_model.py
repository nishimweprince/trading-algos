from __future__ import annotations

import json
from datetime import UTC, datetime

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import settings
from app.db.duck import get_connection, register_features_view
from app.ml.meta.artifact import load_meta_artifact, read_active_shadow
from app.models.meta_model import (
    MetaModelStatusOut,
    MetaReplayInferenceOut,
    MetaShadowEventOut,
    MetaShadowPageOut,
)
from app.services.calendar.features import CALENDAR_MODEL_FEATURES, build_calendar_feature_frame
from app.services.calendar.store import calendar_manifest_path
from app.services.market_execution import execution_status
from app.services.meta_events import (
    HORIZON,
    STOP_ATR,
    TARGET_ATR,
    _canonical_features,
    indicative_price_levels,
)
from app.services.meta_shadow_store import MetaShadowStore
from app.utils.time import to_utc

router = APIRouter(tags=["meta-model"])


def get_db():
    con = get_connection()
    register_features_view(con)
    try:
        yield con
    finally:
        con.close()


def _json_file(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _causal_event(event: dict, as_of: datetime) -> dict:
    event = dict(event)
    resolved_at = event.get("resolved_at")
    if resolved_at and to_utc(datetime.fromisoformat(resolved_at)) > as_of:
        for name in (
            "exit_ts",
            "outcome",
            "net_r_3",
            "net_r_5",
            "net_r_8",
            "exit_price",
            "gross_r",
            "bars_to_resolution",
            "ambiguous_bar",
        ):
            event[name] = None
    allowed = set(MetaShadowEventOut.model_fields)
    return {name: value for name, value in event.items() if name in allowed}


def _normalise(value):
    if value is pd.NA:
        return None
    if hasattr(value, "tolist"):
        value = value.tolist()
    elif hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and pd.isna(value):
        return None
    return value


@router.get("/meta-model/replay", response_model=MetaReplayInferenceOut)
def get_meta_replay(
    symbol: str = Query(...),  # noqa: B008
    timeframe: str = Query(...),  # noqa: B008
    signal_ts: datetime = Query(...),  # noqa: B008
    side: int = Query(..., ge=-1, le=1),  # noqa: B008
    con=Depends(get_db),  # noqa: B008
):
    """Causal research score for Replay; never reads an outcome or places an order."""
    if side not in {-1, 1}:
        raise HTTPException(status_code=422, detail="side must be -1 or 1")
    symbol, timeframe, stamp = symbol.upper(), timeframe.upper(), to_utc(signal_ts)
    frame = con.execute(
        "SELECT * FROM bar_features WHERE symbol=? AND timeframe=? AND ts=? LIMIT 1",
        [symbol, timeframe, stamp],
    ).df()
    if frame.empty:
        raise HTTPException(status_code=404, detail="No causal feature row for this replay bar")
    row = frame.iloc[0]
    if not bool(row.get("data_quality_reliable", False)):
        raise HTTPException(status_code=409, detail="Replay bar failed data-quality gates")
    if not bool(row.get("context_reliable", False)):
        raise HTTPException(status_code=409, detail="Replay bar lacks reliable causal context")

    try:
        levels = indicative_price_levels(float(row["close"]), float(row["atr_at_bar"]), side)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409, detail="Replay bar lacks a reliable price or ATR"
        ) from exc

    base = {name: _normalise(value) for name, value in _canonical_features(row, side).items()}
    calendar = (
        build_calendar_feature_frame(
            [pd.Timestamp(stamp)],
            currencies=settings.calendar_symbol_currencies.get(symbol, ["USD"]),
        )
        .iloc[0]
        .to_dict()
    )
    coverage_ok = bool(calendar.pop("calendar_coverage_ok"))
    if not coverage_ok:
        raise HTTPException(status_code=409, detail="Calendar coverage is unavailable")
    v2 = {
        **base,
        **{name: _normalise(calendar[name]) for name in CALENDAR_MODEL_FEATURES},
    }
    pointer = read_active_shadow()
    if not pointer:
        raise HTTPException(status_code=503, detail="Meta shadow artifacts are unavailable")
    versions = [(pointer["active_version"], "active")]
    challenger_version = pointer.get("challenger_version")
    if challenger_version and challenger_version != pointer["active_version"]:
        versions.append((challenger_version, "challenger"))
    predictions = []
    for version, role in versions:
        model, metadata = load_meta_artifact(version)
        if not isinstance(metadata.get("orders_enabled"), bool):
            raise HTTPException(status_code=503, detail="Artifact lacks an execution policy")
        feature_version = int(metadata["meta_feature_version"])
        features = base if feature_version == 1 else v2
        if set(features) != set(metadata["feature_columns"]):
            raise HTTPException(status_code=503, detail="Replay feature schema mismatch")
        probability = float(model.predict_proba(pd.DataFrame([features]))[0])
        threshold = float(metadata["threshold"])
        predictions.append(
            {
                "artifact_version": version,
                "role": role,
                "meta_feature_version": feature_version,
                "probability": probability,
                "threshold": threshold,
                "would_take": probability >= threshold,
                "target_take_rate": metadata.get("target_take_rate"),
            }
        )
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "signal_ts": stamp,
        "side": side,
        "status": "research_shadow",
        "orders_enabled": bool(pointer.get("orders_enabled")),
        "calendar_coverage_ok": True,
        "predictions": predictions,
        "indicative_levels": {
            "basis": "signal_close",
            **levels,
            "final_levels_pending": True,
        },
        "contract": {
            "entry": "next_h1_open",
            "stop_atr": STOP_ATR,
            "target_atr": TARGET_ATR,
            "horizon_bars": HORIZON,
        },
    }


@router.get("/meta-model/shadow", response_model=MetaShadowEventOut)
def get_meta_shadow(
    symbol: str = Query(...),  # noqa: B008
    timeframe: str = Query(...),  # noqa: B008
    signal_ts: datetime = Query(...),  # noqa: B008
):
    event = MetaShadowStore(settings.meta_shadow_db_path).event_by_signal(
        symbol=symbol.upper(), timeframe=timeframe.upper(), signal_ts=to_utc(signal_ts)
    )
    if event is None:
        raise HTTPException(status_code=404, detail="No live meta-event at that signal timestamp")
    if event["state"] == "ineligible":
        raise HTTPException(status_code=409, detail=event["ineligible_reason"])
    return _causal_event(event, datetime.now(UTC))


@router.get("/meta-model/shadow/history", response_model=MetaShadowPageOut)
def get_meta_shadow_history(
    symbol: str = Query("XAUUSD"),  # noqa: B008
    timeframe: str = Query("H1"),  # noqa: B008
    offset: int = Query(0, ge=0),  # noqa: B008
    limit: int = Query(100, ge=1, le=200),  # noqa: B008
    as_of: datetime | None = Query(None),  # noqa: B008
    forward_only: bool = Query(False),  # noqa: B008
):
    items, total = MetaShadowStore(settings.meta_shadow_db_path).events(
        symbol=symbol.upper(),
        timeframe=timeframe.upper(),
        offset=offset,
        limit=limit,
        forward_only=forward_only,
    )
    cutoff = to_utc(as_of) if as_of else datetime.now(UTC)
    return {
        "items": [_causal_event(event, cutoff) for event in items],
        "offset": offset,
        "limit": limit,
        "total": total,
    }


@router.get("/meta-model/status", response_model=MetaModelStatusOut)
def get_meta_model_status():
    sources = settings.data_dir / "candle_sources"
    pointer = read_active_shadow()
    store = MetaShadowStore(settings.meta_shadow_db_path)
    return {
        "status": (
            "execution_enabled" if pointer and pointer.get("orders_enabled") else "research_shadow"
        ),
        "orders_enabled": bool(pointer and pointer.get("orders_enabled")),
        "active_shadow": pointer,
        "ledger": store.status(),
        "execution": execution_status(settings, store),
        "capital_boundary": _json_file(sources / "capital_boundary.json"),
        "capital_publish": _json_file(sources / "capital_publish.json"),
        "calendar_manifest": _json_file(calendar_manifest_path()),
    }
