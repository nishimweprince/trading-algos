from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import settings
from app.db.duck import get_connection, register_candles_view
from app.models.trade import OccurrenceOut, TradeSubmit
from app.services.occurrences import list_occurrences, process_trade

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
    date_from = body.date_from or body.signal_ts
    date_to = body.date_to or (body.signal_ts + timedelta(hours=settings.max_bars * 24))

    try:
        return process_trade(
            con,
            session_id=body.session_id,
            symbol=body.symbol,
            timeframe=body.timeframe,
            signal_ts=body.signal_ts,
            setup_id=body.setup_id,
            side=body.side,
            entry=body.entry,
            sl=body.sl,
            tp=body.tp,
            notes=body.notes,
            calendar_flag=body.calendar_flag,
            calendar_tags=body.calendar_tags,
            observed_result=body.observed_result,
            date_from=date_from,
            date_to=date_to,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/trades", response_model=list[OccurrenceOut])
def get_trades(session_id: str | None = Query(None), con=Depends(get_db)) -> list[dict]:
    return list_occurrences(con, session_id)
