from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.duck import get_connection, register_candles_view
from app.models.meta_event import MetaEventReviewIn
from app.services.meta_event_review import causal_detail, list_events, reveal, save_review, summary

router = APIRouter(prefix="/meta-events", tags=["meta-events"])


def get_db():
    con = get_connection()
    register_candles_view(con)
    try:
        yield con
    finally:
        con.close()


@router.get("")
def get_meta_events(
    symbol: str = Query("XAUUSD"),
    timeframe: str = Query("H1"),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    year: int | None = None,
    setup: str | None = None,
    side: int | None = Query(None, ge=-1, le=1),
    confidence_min: float | None = Query(None, ge=0, le=1),
    quality: str | None = None,
    review_status: str | None = None,
    sort: str = Query("signal_ts", pattern="^(signal_ts|confidence)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    con=Depends(get_db),
):
    return list_events(
        con, symbol=symbol, timeframe=timeframe, offset=offset, limit=limit,
        year=year, setup=setup, side=side, confidence_min=confidence_min,
        quality=quality, review_status=review_status, sort=sort, order=order,
    )


@router.get("/summary")
def get_meta_event_summary(
    symbol: str = Query("XAUUSD"), timeframe: str = Query("H1"), con=Depends(get_db)
):
    return summary(con, symbol, timeframe)


@router.get("/{event_id}")
def get_meta_event(event_id: str, con=Depends(get_db)):
    event = causal_detail(con, event_id)
    if event is None:
        raise HTTPException(404, "Automated event not found or export has not been built")
    return event


@router.post("/{event_id}/review")
def review_meta_event(event_id: str, body: MetaEventReviewIn, con=Depends(get_db)):
    try:
        return save_review(
            con, event_id, verdict=body.verdict, notes=body.notes, phase=body.phase
        )
    except KeyError:
        raise HTTPException(404, "Automated event not found") from None
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/{event_id}/reveal")
def reveal_meta_event(event_id: str, con=Depends(get_db)):
    try:
        return reveal(con, event_id)
    except KeyError:
        raise HTTPException(404, "Automated event not found") from None
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from None
