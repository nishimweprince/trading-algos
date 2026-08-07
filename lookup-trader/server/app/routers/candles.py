from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.duck import get_connection, register_candles_view
from app.models.trade import CandleBoundsOut, CandleOut, CandlePageOut
from app.services.candles import (
    candles_to_records,
    candle_count,
    fetch_candle_bounds,
    fetch_candle_page,
    fetch_candles,
    fetch_symbols,
    fetch_timeframes,
)
from app.utils.time import to_utc_iso

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


@router.get("/candles/bounds", response_model=CandleBoundsOut)
def get_candle_bounds(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    con=Depends(get_db),
) -> dict[str, object]:
    return fetch_candle_bounds(con, symbol, timeframe)


@router.get("/candles", response_model=list[CandleOut])
def get_candles(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    date_from: datetime = Query(...),
    date_to: datetime = Query(...),
    con=Depends(get_db),
) -> list[dict]:
    if candle_count(con, symbol, timeframe, date_from, date_to) > 10_000:
        raise HTTPException(
            status_code=422,
            detail="This range exceeds 10,000 candles; use /candles/page with bounded pagination.",
        )
    df = fetch_candles(con, symbol, timeframe, date_from, date_to)
    return candles_to_records(df)


@router.get("/candles/page", response_model=CandlePageOut)
def get_candle_page(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    date_from: datetime = Query(...),
    date_to: datetime = Query(...),
    offset: int = Query(0, ge=0),
    limit: int = Query(2048, ge=1, le=2048),
    con=Depends(get_db),
) -> dict:
    frame, total, minimum, maximum = fetch_candle_page(
        con, symbol, timeframe, date_from, date_to, offset, limit
    )
    return {
        "items": candles_to_records(frame),
        "offset": offset,
        "limit": limit,
        "total": total,
        "has_previous": offset > 0,
        "has_next": offset + len(frame) < total,
        "min_ts": to_utc_iso(minimum) if minimum is not None else None,
        "max_ts": to_utc_iso(maximum) if maximum is not None else None,
    }
