"""Immutable outcome-model artifact persistence and validation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import joblib

_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_artifact(
    root: Path,
    version: str,
    *,
    model: object,
    metadata: dict[str, Any],
    metrics: dict[str, Any],
    dataset_manifest: dict[str, Any],
) -> Path:
    """Atomically create a version directory and refuse every overwrite."""
    if not _VERSION.fullmatch(version):
        raise ValueError(
            "Artifact version may only contain letters, numbers, dot, dash, underscore"
        )
    destination = root / version
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite immutable artifact: {destination}")
    root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{version}-", dir=root))
    try:
        model_path = temporary / "model.joblib"
        joblib.dump(model, model_path, compress=3)
        complete_metadata = {
            **metadata,
            "artifact_version": version,
            "model_sha256": file_sha256(model_path),
        }
        (temporary / "metadata.json").write_text(
            json.dumps(complete_metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (temporary / "metrics.json").write_text(
            json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (temporary / "dataset_manifest.json").write_text(
            json.dumps(dataset_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.rename(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def load_artifact(
    path: Path, *, expected_version: str | None = None, expected_schema_hash: str | None = None
) -> tuple[object, dict[str, Any]]:
    """Load only an internally consistent artifact matching caller expectations."""
    metadata = _json(path / "metadata.json")
    actual_version = metadata.get("artifact_version")
    if actual_version != path.name:
        raise ValueError(
            f"Artifact version mismatch: directory={path.name!r}, metadata={actual_version!r}"
        )
    if expected_version is not None and actual_version != expected_version:
        raise ValueError(
            f"Artifact version mismatch: expected={expected_version!r}, actual={actual_version!r}"
        )
    actual_schema = metadata.get("schema_sha256")
    if expected_schema_hash is not None and actual_schema != expected_schema_hash:
        raise ValueError(
            f"Artifact schema mismatch: expected={expected_schema_hash!r}, actual={actual_schema!r}"
        )
    model_path = path / "model.joblib"
    if file_sha256(model_path) != metadata.get("model_sha256"):
        raise ValueError("Artifact model checksum mismatch")
    return joblib.load(model_path), metadata
