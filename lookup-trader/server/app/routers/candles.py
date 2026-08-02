from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.duck import get_connection, register_candles_view
from app.models.trade import CandleOut
from app.services.candles import candles_to_records, fetch_candles, fetch_symbols, fetch_timeframes

router = APIRouter(tags=["candles"])


def get_db():
    con = get_connection()
    register_candles_view(con)
    try:
        yield con
    finally:
        con.close()


@router.get("/symbols")
def list_symbols(con=Depends(get_db)) -> list[str]:
    return fetch_symbols(con)


@router.get("/timeframes")
def list_timeframes(symbol: str = Query(...), con=Depends(get_db)) -> list[str]:
    return fetch_timeframes(con, symbol)


@router.get("/candles", response_model=list[CandleOut])
def get_candles(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    date_from: datetime = Query(...),
    date_to: datetime = Query(...),
    con=Depends(get_db),
) -> list[dict]:
    df = fetch_candles(con, symbol, timeframe, date_from, date_to)
    return candles_to_records(df)
