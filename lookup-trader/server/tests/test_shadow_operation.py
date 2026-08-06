from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services.pipeline_lock import PipelineLockedError, pipeline_lock
from app.services.shadow_store import ShadowStore
from app.services.shadow_worker import _outcome


def _prediction(ts: datetime) -> dict:
    return {
        "artifact_version": "candidate-v1",
        "model_version": "outcome-v1",
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "ts": ts,
        "side": 1,
        "direction": "long",
        "p_win": 0.5,
        "p_loss": 0.3,
        "p_timeout": 0.2,
        "expected_gross_r": 0.45,
        "expected_net_r": 0.42,
        "observed_spread": 0.3,
        "action_threshold_r": 0.0,
        "would_trade": True,
        "empirical_base_rate_json": {"status": "ok", "win_rate": 0.44},
        "tags_json": [{"setup_id": "inside_break"}],
        "schema_sha256": "schema",
        "feature_version": "2.0.0",
        "bar_feature_version": "1.2.0",
        "training_source": "histdata",
        "live_source": "capital",
        "source_boundary": "2026-08-01T00:00:00+00:00",
        "created_at": ts,
    }


def test_shadow_store_is_wal_idempotent_and_reveal_gated(tmp_path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    ts = datetime(2026, 8, 2, 1, tzinfo=UTC)
    assert store.insert_predictions([_prediction(ts)]) == 1
    assert store.insert_predictions([_prediction(ts)]) == 0
    with store.connect() as con:
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    as_of = ts + timedelta(hours=24)
    assert store.resolve(
        artifact_version="candidate-v1",
        symbol="XAUUSD",
        timeframe="H1",
        ts=ts.isoformat(),
        side=1,
        outcome="win",
        as_of_ts=as_of,
    )
    hidden = store.history(
        symbol="XAUUSD",
        timeframe="H1",
        date_from=ts - timedelta(hours=1),
        date_to=as_of + timedelta(hours=1),
        revealed_through=as_of - timedelta(seconds=1),
    )
    assert hidden[0]["outcome"] is None
    revealed = store.history(
        symbol="XAUUSD",
        timeframe="H1",
        date_from=ts - timedelta(hours=1),
        date_to=as_of + timedelta(hours=1),
        revealed_through=as_of,
    )
    assert revealed[0]["outcome"] == "win"


def test_shadow_history_api_enforces_revealed_through(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "shadow_db_path", tmp_path / "shadow.sqlite3")
    store = ShadowStore(settings.shadow_db_path)
    ts = datetime(2026, 8, 2, 1, tzinfo=UTC)
    store.insert_predictions([_prediction(ts)])
    store.resolve(
        artifact_version="candidate-v1",
        symbol="XAUUSD",
        timeframe="H1",
        ts=ts.isoformat(),
        side=1,
        outcome="loss",
        as_of_ts=ts + timedelta(hours=24),
    )
    response = TestClient(app).get(
        "/outcome-model/shadow/history",
        params={
            "date_from": (ts - timedelta(hours=1)).isoformat(),
            "date_to": (ts + timedelta(hours=30)).isoformat(),
            "revealed_through": (ts + timedelta(hours=23)).isoformat(),
        },
    )
    assert response.status_code == 200
    assert response.json()[0]["outcome"] is None


def test_outcome_requires_24_subsequent_complete_bars_and_is_conservative():
    neutral = pd.DataFrame({"high": [100.5] * 24, "low": [99.5] * 24})
    assert _outcome(neutral.iloc[:23], side=1, entry=100.0, atr=1.0) == "timeout"
    win = neutral.copy()
    win.loc[23, "high"] = 101.5
    assert _outcome(win, side=1, entry=100.0, atr=1.0) == "win"
    ambiguous = neutral.copy()
    ambiguous.loc[0, ["high", "low"]] = [101.5, 99.0]
    assert _outcome(ambiguous, side=1, entry=100.0, atr=1.0) == "loss"


def test_worker_process_lock_rejects_duplicate_process(tmp_path):
    path = tmp_path / "worker.lock"
    with pipeline_lock(path):
        with pytest.raises(PipelineLockedError):
            with pipeline_lock(path):
                pass
