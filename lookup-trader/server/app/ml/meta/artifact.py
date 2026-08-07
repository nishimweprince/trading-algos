"""Immutable binary meta-model artifacts and atomic shadow pointer."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from app.config import settings
from app.ml.outcome.artifact import load_artifact, save_artifact


def active_shadow_path() -> Path:
    return settings.meta_artifact_root / "active-shadow.json"


def read_active_shadow() -> dict[str, Any] | None:
    path = active_shadow_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def write_active_shadow(payload: dict[str, Any]) -> None:
    path = active_shadow_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_meta_artifact(version: str):
    return load_artifact(settings.meta_artifact_root / version, expected_version=version)


__all__ = [
    "active_shadow_path",
    "load_meta_artifact",
    "read_active_shadow",
    "save_artifact",
    "write_active_shadow",
]
