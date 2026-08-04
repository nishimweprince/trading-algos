from __future__ import annotations

import base64
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.db.duck import get_connection, register_candles_view

router = APIRouter(tags=["screenshots"])

_SAFE_SEGMENT = re.compile(r"[^a-zA-Z0-9_-]+")


def _safe_segment(value: str) -> str:
    return _SAFE_SEGMENT.sub("_", value).strip("_") or "unknown"


def screenshots_dir() -> Path:
    path = settings.data_dir / "screenshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_db():
    con = get_connection()
    register_candles_view(con)
    try:
        yield con
    finally:
        con.close()


class ScreenshotUpload(BaseModel):
    session_id: str
    trade_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: str = Field(..., pattern="^(entry|exit)$")
    image_base64: str


class ScreenshotOut(BaseModel):
    path: str
    trade_id: str


@router.post("/screenshots", response_model=ScreenshotOut)
def upload_screenshot(body: ScreenshotUpload, _con=Depends(get_db)) -> dict:
    try:
        raw = body.image_base64
        if "," in raw:
            raw = raw.split(",", 1)[1]
        image_bytes = base64.b64decode(raw)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail="Invalid image_base64 payload") from e

    session_seg = _safe_segment(body.session_id)
    trade_seg = _safe_segment(body.trade_id)
    dest_dir = screenshots_dir() / session_seg
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{trade_seg}_{body.kind}.png"
    dest_path = dest_dir / filename
    dest_path.write_bytes(image_bytes)

    rel_path = f"screenshots/{session_seg}/{filename}"
    return {"path": rel_path, "trade_id": body.trade_id}


@router.get("/screenshots/{session_id}/{filename}")
def get_screenshot(session_id: str, filename: str):
    session_seg = _safe_segment(session_id)
    safe_name = Path(filename).name
    if safe_name != filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    path = screenshots_dir() / session_seg / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Screenshot not found")

    from fastapi.responses import FileResponse

    return FileResponse(path, media_type="image/png")
