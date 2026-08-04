from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.duck import get_connection
from app.services.calendar.store import calendar_flags, list_events, register_events_view
from app.utils.time import to_utc

router = APIRouter(tags=["calendar"])


def get_db():
    con = get_connection()
    register_events_view(con)
    try:
        yield con
    finally:
        con.close()


@router.get("/calendar/events")
def get_events(date: date = Query(...), con=Depends(get_db)) -> list[dict]:
    return list_events(con, date)


@router.get("/calendar/flags")
def get_flags(
    symbol: str = Query(...),
    ts: datetime = Query(...),
    con=Depends(get_db),
) -> dict:
    try:
        return calendar_flags(con, symbol.upper(), to_utc(ts))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc
