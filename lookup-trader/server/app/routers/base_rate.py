from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import settings
from app.db.duck import get_connection, register_candles_view, register_features_view
from app.models.base_rate import BaseRateOut, BaseRateTagState
from app.services.bar_features import compute_htf_context
from app.services.bar_series import fetch_bar_series
from app.services.base_rate import base_rate_with_recommendation, context_from_bar, store_is_built
from app.services.candles import fetch_labeling_window
from app.services.context import compute_context
from app.utils.time import to_utc

router = APIRouter(tags=["base-rate"])


def effective_base_rate_min_samples(requested: int | None) -> int:
    """A caller may demand more evidence, never less than the server floor."""
    return max(requested or settings.base_rate_min_samples, settings.base_rate_min_samples)


def get_db():
    con = get_connection()
    register_candles_view(con)
    register_features_view(con)
    try:
        yield con
    finally:
        con.close()


@router.get("/bar-features/series")
def get_bar_series(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    date_from: datetime = Query(...),
    date_to: datetime = Query(...),
    revealed_through: datetime | None = Query(None),
    horizon: int = Query(24),
    con=Depends(get_db),
) -> list[dict]:
    """Per-bar features for chart overlays.

    `revealed_through` is the reveal boundary, and the forward half of every row
    past the resulting cutoff comes back null. Omitting it suppresses forward
    data entirely — the safe default, so a caller that forgets the parameter gets
    less information rather than more.
    """
    if not store_is_built(con):
        return []
    try:
        return fetch_bar_series(
            con,
            symbol=symbol,
            timeframe=timeframe,
            date_from=date_from,
            date_to=date_to,
            revealed_through=revealed_through,
            horizon=horizon,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/base-rate", response_model=BaseRateOut)
def get_base_rate(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    signal_ts: datetime = Query(...),
    horizon: int = Query(24),
    target_atr: float = Query(1.5),
    stop_atr: float = Query(1.0),
    side: int | None = Query(None),
    min_samples: int | None = Query(None),
    pinned: list[str] = Query(default=[]),
    tag_setup_id: str | None = Query(None),
    tag_state: BaseRateTagState = Query("complete"),
    apply_cost: bool = Query(True),
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
    context = context_from_bar(ctx, htf, bar_ts)
    if tag_setup_id:
        context["tag_setup_id"] = tag_setup_id
        context["tag_state"] = tag_state

    try:
        # The query parameter remains useful for asking for *more* evidence, but
        # automatic recommendations may never undercut the configured floor.
        effective_min_samples = effective_base_rate_min_samples(min_samples)
        return base_rate_with_recommendation(
            con,
            symbol=symbol,
            timeframe=timeframe,
            context=context,
            horizon=horizon,
            target_atr=target_atr,
            stop_atr=stop_atr,
            side=side,
            min_samples=effective_min_samples,
            pinned=pinned,
            apply_cost=apply_cost,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
