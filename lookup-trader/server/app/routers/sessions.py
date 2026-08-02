from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.db.duck import get_connection, register_candles_view
from app.models.trade import SessionCreate, SessionOut, SessionPatch
from app.utils.time import to_utc_iso

router = APIRouter(tags=["sessions"])


def get_db():
    con = get_connection()
    register_candles_view(con)
    try:
        yield con
    finally:
        con.close()


def _session_row_to_dict(row) -> dict:
    d = row.to_dict()
    ts_fields = {"started_at", "ended_at", "date_from", "date_to"}
    for k, v in d.items():
        if hasattr(v, "isoformat") and k in ts_fields:
            dt = v.to_pydatetime() if hasattr(v, "to_pydatetime") else v
            d[k] = to_utc_iso(dt)
        elif hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    if "session_id" in d:
        d["session_id"] = str(d["session_id"])
    return d


@router.post("/sessions", response_model=SessionOut)
def create_session(body: SessionCreate, con=Depends(get_db)) -> dict:
    session_id = str(uuid.uuid4())
    con.execute(
        """
        INSERT INTO labeling_sessions (session_id, symbol, timeframe, date_from, date_to, blinded, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            session_id,
            body.symbol,
            body.timeframe,
            body.date_from,
            body.date_to,
            body.blinded,
            body.notes,
        ],
    )
    row = con.execute(
        "SELECT * FROM labeling_sessions WHERE session_id = ?", [session_id]
    ).fetchdf().iloc[0]
    return _session_row_to_dict(row)


@router.patch("/sessions/{session_id}", response_model=SessionOut)
def patch_session(session_id: str, body: SessionPatch, con=Depends(get_db)) -> dict:
    existing = con.execute(
        "SELECT 1 FROM labeling_sessions WHERE session_id = ?", [session_id]
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Session not found")

    if body.ended_at is not None:
        con.execute(
            "UPDATE labeling_sessions SET ended_at = ? WHERE session_id = ?",
            [body.ended_at, session_id],
        )
    if body.notes is not None:
        con.execute(
            "UPDATE labeling_sessions SET notes = ? WHERE session_id = ?",
            [body.notes, session_id],
        )

    row = con.execute(
        "SELECT * FROM labeling_sessions WHERE session_id = ?", [session_id]
    ).fetchdf().iloc[0]
    return _session_row_to_dict(row)
