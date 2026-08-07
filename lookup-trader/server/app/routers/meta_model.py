from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query

from app.config import settings
from app.ml.meta.artifact import read_active_shadow
from app.models.meta_model import MetaModelStatusOut, MetaShadowEventOut, MetaShadowPageOut
from app.services.calendar.store import calendar_manifest_path
from app.services.meta_shadow_store import MetaShadowStore
from app.utils.time import to_utc

router = APIRouter(tags=["meta-model"])


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
        ):
            event[name] = None
    allowed = set(MetaShadowEventOut.model_fields)
    return {name: value for name, value in event.items() if name in allowed}


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
):
    items, total = MetaShadowStore(settings.meta_shadow_db_path).events(
        symbol=symbol.upper(), timeframe=timeframe.upper(), offset=offset, limit=limit
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
    return {
        "status": "research_shadow",
        "orders_enabled": False,
        "active_shadow": read_active_shadow(),
        "ledger": MetaShadowStore(settings.meta_shadow_db_path).status(),
        "capital_boundary": _json_file(sources / "capital_boundary.json"),
        "capital_publish": _json_file(sources / "capital_publish.json"),
        "calendar_manifest": _json_file(calendar_manifest_path()),
    }
