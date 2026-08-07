from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.duck import get_connection
from app.services.calendar.store import (
    CalendarCoverageError,
    calendar_flags,
    list_events,
    register_events_view,
)
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
def get_events(
    date: date = Query(...),  # noqa: B008
    con=Depends(get_db),  # noqa: B008
) -> list[dict]:
    try:
        return list_events(con, date)
    except CalendarCoverageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/calendar/flags")
def get_flags(
    symbol: str = Query(...),  # noqa: B008
    ts: datetime = Query(...),  # noqa: B008
    con=Depends(get_db),  # noqa: B008
) -> dict:
    try:
        return calendar_flags(con, symbol.upper(), to_utc(ts))
    except CalendarCoverageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
