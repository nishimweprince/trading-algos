from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import settings
from app.db.duck import get_connection, register_candles_view, register_features_view
from app.models.base_rate import BaseRateOut
from app.services.bar_features import compute_htf_context
from app.services.base_rate import base_rate, store_is_built
from app.services.candles import fetch_labeling_window
from app.services.context import compute_context
from app.utils.time import to_utc

router = APIRouter(tags=["base-rate"])


def get_db():
    con = get_connection()
    register_candles_view(con)
    register_features_view(con)
    try:
        yield con
    finally:
        con.close()


@router.get("/base-rate", response_model=BaseRateOut)
def get_base_rate(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    signal_ts: datetime = Query(...),
    horizon: int = Query(24),
    target_atr: float = Query(1.5),
    stop_atr: float = Query(1.0),
    side: int = Query(1),
    min_samples: int | None = Query(None),
    pinned: list[str] = Query(default=[]),
    con=Depends(get_db),
) -> dict:
    """What price did next, historically, from bars in this bar's context.

    `forward_bars=0` for the same reason `/context` uses it: the caller is at the
    replay cursor, and the answer must be built from what is already on screen.
    The forward information in the result comes from *other* bars in history, not
    from this one.
    """
    if not store_is_built(con):
        raise HTTPException(
            status_code=503,
            detail="No bar feature store built yet — run scripts/build_bar_features.py",
        )

    ts = to_utc(signal_ts)
    try:
        candles, signal_idx = fetch_labeling_window(
            con, symbol, timeframe, ts, warmup_bars=settings.warmup_bars, forward_bars=0
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    htf = compute_htf_context(con, symbol, timeframe, ts)
    ctx = compute_context(candles, signal_idx, htf_trend_state=htf["trend_state"])

    bar_ts = candles.iloc[signal_idx]["ts"]
    bar_ts = bar_ts.to_pydatetime() if hasattr(bar_ts, "to_pydatetime") else bar_ts
    context = {
        "trend_state": ctx["trend_state"],
        "atr_bucket": ctx["atr_bucket"],
        "session": ctx["session"],
        "rsi_band": ctx["rsi_band"],
        "day_of_week": ctx["day_of_week"],
        "ema_slope_bucket": ctx["ema_slope_bucket"],
        "atr_change_bucket": ctx["atr_change_bucket"],
        "htf_trend_state": htf["trend_state"],
        "htf_atr_bucket": htf["atr_bucket"],
        "session_overlap": settings.session_overlap_start
        <= bar_ts.hour
        < settings.session_overlap_end,
    }

    try:
        return base_rate(
            con,
            symbol=symbol,
            timeframe=timeframe,
            context=context,
            horizon=horizon,
            target_atr=target_atr,
            stop_atr=stop_atr,
            side=side,
            min_samples=min_samples,
            pinned=pinned,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
