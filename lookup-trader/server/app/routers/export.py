from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from app.config import settings
from app.db.duck import get_connection, register_candles_view
from app.services.export import export_occurrences

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
