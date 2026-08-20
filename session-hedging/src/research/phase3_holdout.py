"""Prospective P3H-20260820 holdout lock. Metadata only until the complete manifest exists."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from models import Candle

HOLDOUT_ID = "P3H-20260820"
HOLDOUT_BAR_COUNT = 4000
HOLDOUT_AFTER_TS = datetime(2026, 8, 20, 10, 45, tzinfo=UTC)
PROTOCOL_COMMIT = "27a85ef"
HOLDOUT_RELATIVE_PATH = "candles/XAUUSD/M15-P3H-20260820.jsonl"
MANIFEST_STEM = "phase3-holdout-manifest"

REQUIRED_MANIFEST_FIELDS: tuple[str, ...] = (
    "protocol_commit",
    "implementation_commit",
    "passing_test_commit",
    "complete_development_report_commit",
    "candidate_list_hash",
    "raw_development_sha256",
    "canonical_development_sha256",
    "holdout_sha256",
    "selected_coordinate_id",
)


class HoldoutLockedError(RuntimeError):
    """Strategy evaluation of the prospective holdout is forbidden."""


def holdout_unlock_errors(manifest: Mapping[str, Any] | None) -> list[str]:
    if manifest is None:
        return ["holdout unlock requires a complete §8.0 manifest"]
    errors: list[str] = []
    for field in REQUIRED_MANIFEST_FIELDS:
        value = manifest.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f"missing {field}")
    protocol = str(manifest.get("protocol_commit", "")).strip()
    if protocol and not protocol.startswith(PROTOCOL_COMMIT):
        errors.append(f"protocol_commit must be {PROTOCOL_COMMIT}")
    return errors


def assert_holdout_locked(
    *, manifest: Mapping[str, Any] | None = None, evaluating_strategy: bool = True
) -> None:
    """Refuse strategy evaluation unless every required unlock field is present."""
    if not evaluating_strategy:
        return
    errors = holdout_unlock_errors(manifest)
    if errors:
        raise HoldoutLockedError("P3H-20260820 remains locked: " + "; ".join(errors))


def holdout_path(data_dir: Path) -> Path:
    return data_dir / HOLDOUT_RELATIVE_PATH


def load_holdout_manifest(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return None
    return payload


def inspect_holdout_file(path: Path, candles: list[Candle] | None = None) -> dict[str, Any]:
    """Hash and bound-check holdout bars. Does not compute strategy metrics."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    meta: dict[str, Any] = {
        "holdout_id": HOLDOUT_ID,
        "path": str(path),
        "exists": True,
        "raw_sha256": digest.hexdigest(),
        "strategy_metrics_computed": False,
    }
    loaded = candles if candles is not None else _load_holdout_candles(path)
    first = loaded[0].ts.astimezone(UTC) if loaded else None
    last = loaded[-1].ts.astimezone(UTC) if loaded else None
    meta.update(
        {
            "bar_count": len(loaded),
            "first_bar_ts": first.isoformat() if first else None,
            "last_bar_ts": last.isoformat() if last else None,
            "after_development_end": first is not None and first > HOLDOUT_AFTER_TS,
            "complete": len(loaded) == HOLDOUT_BAR_COUNT,
        }
    )
    return meta


def _load_holdout_candles(path: Path) -> list[Candle]:
    candles: list[Candle] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            candles.append(Candle.model_validate_json(text))
    return candles


def holdout_ready_errors(*, manifest: Mapping[str, Any] | None, bar_count: int | None) -> list[str]:
    errors = list(holdout_unlock_errors(manifest))
    if bar_count is None:
        errors.append(f"{HOLDOUT_ID} bars are unavailable")
    elif bar_count != HOLDOUT_BAR_COUNT:
        errors.append(f"{HOLDOUT_ID} has {bar_count} bars, not {HOLDOUT_BAR_COUNT}")
    return errors
