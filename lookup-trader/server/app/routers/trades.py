from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.duck import get_connection, register_candles_view
from app.models.trade import OccurrenceOut, OccurrencePatch, TradeSubmit
from app.services.occurrences import (
    exclude_occurrence,
    list_occurrences,
    patch_occurrence,
    process_trade,
)
from app.utils.time import to_utc

router = APIRouter(tags=["trades"])


def get_db():
    con = get_connection()
    register_candles_view(con)
    try:
        yield con
    finally:
        con.close()


@router.post("/trades", response_model=OccurrenceOut)
def submit_trade(body: TradeSubmit, con=Depends(get_db)) -> dict:
    provenance = body.provenance
    try:
        return process_trade(
            con,
            session_id=body.session_id,
            symbol=body.symbol,
            timeframe=body.timeframe,
            signal_ts=to_utc(body.signal_ts),
            setup_id=body.setup_id,
            side=body.side,
            entry=body.entry,
            sl=body.sl,
            tp=body.tp,
            outcome_kind=body.outcome_kind,
            skip_reason=body.skip_reason,
            notes=body.notes,
            calendar_flag=body.calendar_flag,
            calendar_tags=body.calendar_tags,
            observed_result=body.observed_result,
            observed_trend=body.observed_trend,
            confluence_tags=body.confluence_tags,
            session_override=body.session,
            pips_captured=body.pips_captured,
            screenshot_entry=body.screenshot_entry,
            screenshot_exit=body.screenshot_exit,
            metadata=body.metadata,
            blinded=body.blinded,
            peeked=provenance.peeked if provenance else None,
            provenance=provenance.model_dump(exclude_none=True) if provenance else None,
        )
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/trades", response_model=list[OccurrenceOut])
def get_trades(session_id: str | None = Query(None), con=Depends(get_db)) -> list[dict]:
    return list_occurrences(con, session_id)


@router.patch("/trades/{occurrence_id}", response_model=OccurrenceOut)
def update_trade(occurrence_id: str, body: OccurrencePatch, con=Depends(get_db)) -> dict:
    updated = patch_occurrence(con, occurrence_id, body.model_dump(exclude_unset=True))
    if updated is None:
        raise HTTPException(status_code=404, detail="Occurrence not found")
    return updated


@router.delete("/trades/{occurrence_id}", response_model=OccurrenceOut)
def delete_trade(occurrence_id: str, reason: str | None = Query(None), con=Depends(get_db)) -> dict:
    """Soft delete. Labelled data is expensive; excluded rows stay auditable."""
    updated = exclude_occurrence(con, occurrence_id, reason)
    if updated is None:
        raise HTTPException(status_code=404, detail="Occurrence not found")
    return updated
