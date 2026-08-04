from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.duck import get_connection, register_candles_view
from app.models.trade import SignalOut, SignalSubmit
from app.services.pending import resolve_pending_occurrences
from app.services.signals import list_signals, process_signal
from app.utils.time import to_utc

router = APIRouter(tags=["signals"])


def get_db():
    con = get_connection()
    register_candles_view(con)
    try:
        yield con
    finally:
        con.close()


@router.post("/signals", response_model=SignalOut)
def create_signal(body: SignalSubmit, con=Depends(get_db)) -> dict:
    annotations = body.annotations
    compare_ctx = body.compare_context.model_dump(exclude_none=True) if body.compare_context else None
    try:
        return process_signal(
            con,
            session_id=body.session_id,
            symbol=body.symbol,
            timeframe=body.timeframe,
            signal_ts=to_utc(body.signal_ts),
            setup_id=body.setup_id,
            side=body.side,
            cursor_idx=body.cursor_idx,
            bars_visible=body.bars_visible,
            peeked=body.peeked,
            blinded=body.blinded,
            confluence_tags=annotations.confluence_tags if annotations else None,
            calendar_flag=annotations.calendar_flag if annotations else None,
            calendar_tags=annotations.calendar_tags if annotations else None,
            confidence=annotations.confidence if annotations else None,
            at_key_level=annotations.at_key_level if annotations else None,
            level_type=annotations.level_type if annotations else None,
            consolidation_before=annotations.consolidation_before if annotations else None,
            annotation_sources=annotations.annotation_sources if annotations else None,
            compare_context=compare_ctx,
            compare_pinned=body.compare_pinned,
            compare_min_samples=body.compare_min_samples,
            compare_source=body.compare_source,
            screenshot_signal=body.screenshot_signal,
            chart_render_spec=body.chart_render_spec,
        )
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/signals", response_model=list[SignalOut])
def get_signals(session_id: str | None = Query(None), con=Depends(get_db)) -> list[dict]:
    return list_signals(con, session_id)


@router.post("/signals/resolve-pending")
def resolve_pending(con=Depends(get_db)) -> dict:
    """Batch-promote pending occurrences once their forward window is complete."""
    resolved = resolve_pending_occurrences(con)
    return {"resolved_count": len(resolved), "resolved_ids": [r["id"] for r in resolved]}
