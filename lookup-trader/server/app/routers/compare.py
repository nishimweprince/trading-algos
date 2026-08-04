from __future__ import annotations

from fastapi import APIRouter, Depends

from app.db.duck import get_connection, register_candles_view
from app.models.trade import CompareRequest, CompareResponse
from app.services.compare import compare_occurrences

router = APIRouter(tags=["compare"])


def get_db():
    con = get_connection()
    register_candles_view(con)
    try:
        yield con
    finally:
        con.close()


@router.post("/compare", response_model=CompareResponse)
def compare(body: CompareRequest, con=Depends(get_db)) -> dict:
    return compare_occurrences(
        con,
        setup_id=body.setup_id,
        symbol=body.symbol,
        timeframe=body.timeframe,
        context=body.context.model_dump(exclude_none=True),
        source=body.source,
        min_samples=body.min_samples,
        pinned=body.pinned,
        exclude_peeked=body.exclude_peeked,
        blinded_only=body.blinded_only,
    )
