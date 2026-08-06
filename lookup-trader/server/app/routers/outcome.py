from __future__ import annotations

from datetime import datetime

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import settings
from app.db.duck import get_connection, register_candles_view
from app.ml.outcome.infer import OutcomeArtifactError, infer_outcomes
from app.models.outcome import OutcomeInferenceOut, OutcomeUnavailableOut
from app.services.bar_features import compute_htf_context
from app.services.pips import pip_size
from app.utils.time import to_utc, to_utc_iso

router = APIRouter(tags=["outcome-model"])


def get_db():
    con = get_connection()
    register_candles_view(con)
    try:
        yield con
    finally:
        con.close()


def fetch_closed_bar_window(con, symbol: str, timeframe: str, signal_ts: datetime) -> pd.DataFrame:
    """Fetch only bars at or before the requested closed bar."""
    frame = con.execute(
        """
        SELECT ts, open, high, low, close, volume FROM (
          SELECT ts, open, high, low, close, volume
          FROM candles
          WHERE symbol = ? AND timeframe = ? AND ts <= ?
          ORDER BY ts DESC
          LIMIT ?
        ) ORDER BY ts ASC
        """,
        [symbol, timeframe, signal_ts, settings.warmup_bars],
    ).df()
    if frame.empty:
        raise ValueError(
            f"No candles at or before {to_utc_iso(signal_ts)} for {symbol} {timeframe}"
        )
    anchor = frame.iloc[-1]["ts"]
    anchor = anchor.to_pydatetime() if hasattr(anchor, "to_pydatetime") else anchor
    if to_utc(anchor) != signal_ts:
        raise ValueError(
            f"signal_ts {to_utc_iso(signal_ts)} is not a bar in {symbol} {timeframe}"
        )
    return frame


@router.get(
    "/outcome-model/shadow",
    response_model=OutcomeInferenceOut,
    responses={
        503: {
            "model": OutcomeUnavailableOut,
            "description": "Outcome artifact absent or incompatible",
        }
    },
)
def get_outcome_shadow(
    symbol: str = Query(...),  # noqa: B008
    timeframe: str = Query(...),  # noqa: B008
    signal_ts: datetime = Query(...),  # noqa: B008
    con=Depends(get_db),  # noqa: B008
):
    """Read-only pilot inference; never contributes to a recommendation."""
    ts = to_utc(signal_ts)
    try:
        window = fetch_closed_bar_window(con, symbol, timeframe, ts)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    htf = compute_htf_context(con, symbol, timeframe, ts)
    try:
        return infer_outcomes(window, symbol, timeframe, pip_size(symbol), htf)
    except OutcomeArtifactError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": str(exc), "retryable": False},
        ) from exc
