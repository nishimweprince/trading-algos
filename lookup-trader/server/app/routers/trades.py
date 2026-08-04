from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import settings
from app.db.duck import get_connection, register_candles_view
from app.models.trade import OccurrenceOut, TradeSubmit
from app.services.occurrences import list_occurrences, process_trade
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
    signal_ts = to_utc(body.signal_ts)
    date_from = to_utc(body.date_from) if body.date_from else signal_ts
    date_to = (
        to_utc(body.date_to)
        if body.date_to
        else signal_ts + timedelta(hours=settings.max_bars * 24)
    )

    try:
        return process_trade(
            con,
            session_id=body.session_id,
            symbol=body.symbol,
            timeframe=body.timeframe,
            signal_ts=signal_ts,
            setup_id=body.setup_id,
            side=body.side,
            entry=body.entry,
            sl=body.sl,
            tp=body.tp,
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
            date_from=date_from,
            date_to=date_to,
        )
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/trades", response_model=list[OccurrenceOut])
def get_trades(session_id: str | None = Query(None), con=Depends(get_db)) -> list[dict]:
    return list_occurrences(con, session_id)
