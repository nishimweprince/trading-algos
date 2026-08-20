"""P3H-20260820 stay locked without the complete §8.0 manifest."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from models import Candle
from research.phase3_holdout import (
    HOLDOUT_BAR_COUNT,
    HOLDOUT_ID,
    HoldoutLockedError,
    assert_holdout_locked,
    holdout_ready_errors,
    inspect_holdout_file,
    load_holdout_manifest,
)


def _bar(ts: datetime) -> Candle:
    return Candle(
        ts=ts,
        open=2000,
        high=2001,
        low=1999,
        close=2000.5,
        volume=1.0,
        provider="test",
        source_instrument="XAUUSD",
    )


def test_metadata_hash_does_not_compute_strategy_metrics(tmp_path: Path) -> None:
    path = tmp_path / "M15-P3H-20260820.jsonl"
    candle = _bar(datetime(2026, 8, 20, 11, 0, tzinfo=UTC))
    path.write_text(candle.model_dump_json() + "\n", encoding="utf-8")
    meta = inspect_holdout_file(path)
    assert meta["holdout_id"] == HOLDOUT_ID
    assert meta["bar_count"] == 1
    assert meta["complete"] is False
    assert meta["strategy_metrics_computed"] is False
    assert meta["after_development_end"] is True
    assert len(meta["raw_sha256"]) == 64


def test_missing_manifest_keeps_strategy_evaluation_locked(tmp_path: Path) -> None:
    assert load_holdout_manifest(tmp_path / "missing.json") is None
    with pytest.raises(HoldoutLockedError, match="locked"):
        assert_holdout_locked(manifest=None, evaluating_strategy=True)
    errors = holdout_ready_errors(manifest=None, bar_count=None)
    assert any("unavailable" in item for item in errors)
    errors = holdout_ready_errors(manifest={"protocol_commit": "27a85ef"}, bar_count=3)
    assert any(f"not {HOLDOUT_BAR_COUNT}" in item for item in errors)
