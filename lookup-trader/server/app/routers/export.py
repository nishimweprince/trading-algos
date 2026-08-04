from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from app.config import settings
from app.db.duck import get_connection, register_candles_view
from app.services.export import export_occurrences
from app.services.export_bar_features import export_bar_features

router = APIRouter(tags=["export"])


def get_db():
    con = get_connection()
    register_candles_view(con)
    try:
        yield con
    finally:
        con.close()


@router.get("/export")
def export(
    format: str = Query("parquet", pattern="^(parquet|csv)$"),
    source: str | None = Query(None),
    include_excluded: bool = Query(False),
    con=Depends(get_db),
) -> FileResponse:
    """Dump occurrences with the JSON columns flattened, for training."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = settings.data_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"occurrences_{stamp}.{format}"

    rows = export_occurrences(con, path, fmt=format, source=source, include_excluded=include_excluded)
    if rows == 0:
        raise HTTPException(status_code=404, detail="No occurrences to export")

    media_type = "text/csv" if format == "csv" else "application/vnd.apache.parquet"
    return FileResponse(path, media_type=media_type, filename=path.name)


@router.get("/export/bar-features")
def export_bar_features_endpoint(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    horizon: int = Query(24),
    target_atr: float = Query(1.5),
    stop_atr: float = Query(1.0),
    side: int = Query(1),
    format: str = Query("parquet", pattern="^(parquet|csv)$"),
    con=Depends(get_db),
) -> FileResponse:
    """Flatten bar_features with a committed y_outcome column."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = settings.data_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"bar_features_{symbol}_{timeframe}_{stamp}.{format}"

    rows = export_bar_features(
        con,
        path,
        symbol=symbol,
        timeframe=timeframe,
        date_from=date_from,
        date_to=date_to,
        horizon=horizon,
        target_atr=target_atr,
        stop_atr=stop_atr,
        side=side,
        fmt=format,
    )
    if rows == 0:
        raise HTTPException(status_code=404, detail="No bar_features rows to export")

    media_type = "text/csv" if format == "csv" else "application/vnd.apache.parquet"
    return FileResponse(path, media_type=media_type, filename=path.name)
