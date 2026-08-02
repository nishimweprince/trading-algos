from __future__ import annotations

from fastapi import APIRouter, Depends

from app.db.duck import get_connection, register_candles_view
from app.models.trade import SetupOut

router = APIRouter(tags=["setups"])


def get_db():
    con = get_connection()
    register_candles_view(con)
    try:
        yield con
    finally:
        con.close()


@router.get("/setups", response_model=list[SetupOut])
def list_setups(con=Depends(get_db)) -> list[dict]:
    df = con.execute(
        "SELECT setup_id, name, description, default_side, category, active "
        "FROM setups WHERE active = TRUE "
        # Grouped picker: categories arrive contiguous so the client can render
        # headings without regrouping.
        "ORDER BY category NULLS LAST, name"
    ).fetchdf()
    return df.to_dict(orient="records")
