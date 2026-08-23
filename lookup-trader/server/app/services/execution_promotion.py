"""Explicit immutable promotion from a research artifact to live execution."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.ml.meta.artifact import (
    load_meta_artifact,
    read_active_shadow,
    save_artifact,
    write_active_shadow,
)


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def promote_execution_artifact(
    *,
    source_version: str,
    new_version: str,
    confirmed: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Copy a proven active artifact and atomically activate its order-enabled copy.

    Research artifacts are immutable. The explicit copy makes the operational
    policy change auditable and leaves the old artifact and rollback pointer
    intact. Environment enablement remains a separate required gate.
    """
    pointer = read_active_shadow()
    if pointer is None or pointer.get("active_version") != source_version:
        raise ValueError("Only the current active artifact can be promoted for execution")
    if pointer.get("orders_enabled") is not False:
        raise ValueError("The active pointer must currently disable orders")
    model, metadata = load_meta_artifact(source_version)
    if metadata.get("orders_enabled") is not False:
        raise ValueError("The source artifact must currently disable orders")
    if metadata.get("promoted") is not True:
        raise ValueError("The source artifact has not passed research promotion gates")
    source_root = settings.meta_artifact_root / source_version
    target_root = settings.meta_artifact_root / new_version
    if target_root.exists():
        raise FileExistsError(f"Refusing to overwrite immutable artifact: {target_root}")
    stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    report = {
        "status": "dry_run" if not confirmed else "promoted_for_execution",
        "source_version": source_version,
        "new_version": new_version,
        "orders_enabled": bool(confirmed),
        "promoted_at": stamp if confirmed else None,
    }
    if not confirmed:
        return report

    promoted_metadata = {
        **metadata,
        "status": "market_execution",
        "orders_enabled": True,
        "execution_promoted_from": source_version,
        "execution_promoted_at": stamp,
    }
    promoted_metadata.pop("artifact_version", None)
    promoted_metadata.pop("model_sha256", None)
    save_artifact(
        settings.meta_artifact_root,
        new_version,
        model=model,
        metadata=promoted_metadata,
        metrics={
            **_json(source_root / "metrics.json"),
            "execution_promotion": report,
        },
        dataset_manifest=_json(source_root / "dataset_manifest.json"),
    )
    _, loaded_metadata = load_meta_artifact(new_version)
    if loaded_metadata.get("orders_enabled") is not True:
        raise RuntimeError("Execution artifact canary failed")
    write_active_shadow(
        {
            **pointer,
            "previous_active_version": source_version,
            "active_version": new_version,
            "reference_version": new_version,
            "activated_at": stamp,
            "orders_enabled": True,
            "status": "market_execution",
            "execution_promoted_from": source_version,
            "execution_promoted_at": stamp,
        }
    )
    return report
