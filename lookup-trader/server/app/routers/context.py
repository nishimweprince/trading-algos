from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import settings
from app.db.duck import get_connection, register_candles_view
from app.models.trade import ContextOut
from app.services.candles import fetch_labeling_window
from app.services.context import compute_context, compute_htf_trend_state
from app.utils.time import to_utc

router = APIRouter(tags=["context"])


def get_db():
    con = get_connection()
    register_candles_view(con)
    try:
        yield con
    finally:
        con.close()


@router.get("/context", response_model=ContextOut)
def get_context(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    signal_ts: datetime = Query(...),
    con=Depends(get_db),
) -> dict:
    """Context features at a bar, so the operator does not have to guess at them.

    `forward_bars=0` is the point of this endpoint: the caller is sitting at the
    replay cursor, and fetching bars past the signal to answer a context query
    would hand the labeler's forward window back to the UI. This must not be able
    to leak what the chart is deliberately hiding.
    """
    try:
        candles, signal_idx = fetch_labeling_window(
            con,
            symbol,
            timeframe,
            to_utc(signal_ts),
            warmup_bars=settings.warmup_bars,
            forward_bars=0,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    ctx = compute_context(
        candles,
        signal_idx,
        htf_trend_state=compute_htf_trend_state(con, symbol, timeframe, to_utc(signal_ts)),
    )
    return {
        "trend_state": ctx["trend_state"],
        "atr_bucket": ctx["atr_bucket"],
        "session": ctx["session"],
        "rsi_band": ctx["rsi_band"],
        "atr_at_signal": ctx["atr_at_signal"],
        "rsi_value": ctx["rsi_value"],
        "dist_ema_atr": ctx["dist_ema_atr"],
        "warmup_bars_available": ctx["warmup_bars_available"],
        "context_reliable": ctx["context_reliable"],
        "day_of_week": ctx["day_of_week"],
        "htf_trend_state": ctx["htf_trend_state"],
        "ema_slope_bucket": ctx["ema_slope_bucket"],
        "atr_change_bucket": ctx["atr_change_bucket"],
        "dist_day_high_atr": ctx["dist_day_high_atr"],
        "dist_day_low_atr": ctx["dist_day_low_atr"],
        "signal_body_pct": ctx["signal_body_pct"],
        "signal_range_atr": ctx["signal_range_atr"],
    }
