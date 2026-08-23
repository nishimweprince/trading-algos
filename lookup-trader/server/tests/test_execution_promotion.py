from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.config import settings
from app.ml.meta.artifact import load_meta_artifact, save_artifact, write_active_shadow
from app.services.execution_promotion import promote_execution_artifact


def _research_artifact(tmp_path, monkeypatch, *, promoted=True):
    monkeypatch.setattr(settings, "meta_artifact_root", tmp_path)
    save_artifact(
        tmp_path,
        "research-v1",
        model={"model": "fixture"},
        metadata={
            "status": "research_shadow",
            "promoted": promoted,
            "orders_enabled": False,
            "meta_feature_version": 1,
        },
        metrics={"accepted": promoted},
        dataset_manifest={"rows": 100},
    )
    write_active_shadow(
        {
            "pointer_version": 1,
            "active_version": "research-v1",
            "reference_version": "research-v1",
            "challenger_version": None,
            "orders_enabled": False,
            "status": "research_shadow",
        }
    )


def test_execution_promotion_is_dry_run_until_explicitly_confirmed(tmp_path, monkeypatch):
    _research_artifact(tmp_path, monkeypatch)

    report = promote_execution_artifact(
        source_version="research-v1",
        new_version="execution-v1",
        confirmed=False,
    )

    assert report["status"] == "dry_run"
    assert report["orders_enabled"] is False
    assert not (tmp_path / "execution-v1").exists()
    pointer = json.loads((tmp_path / "active-shadow.json").read_text())
    assert pointer["active_version"] == "research-v1"
    assert pointer["orders_enabled"] is False


def test_confirmed_promotion_creates_immutable_copy_and_updates_pointer(tmp_path, monkeypatch):
    _research_artifact(tmp_path, monkeypatch)
    stamp = datetime(2026, 8, 23, 12, tzinfo=UTC)

    report = promote_execution_artifact(
        source_version="research-v1",
        new_version="execution-v1",
        confirmed=True,
        now=stamp,
    )

    assert report["status"] == "promoted_for_execution"
    assert (tmp_path / "research-v1" / "metadata.json").exists()
    _, metadata = load_meta_artifact("execution-v1")
    assert metadata["orders_enabled"] is True
    assert metadata["execution_promoted_from"] == "research-v1"
    pointer = json.loads((tmp_path / "active-shadow.json").read_text())
    assert pointer["active_version"] == "execution-v1"
    assert pointer["previous_active_version"] == "research-v1"
    assert pointer["orders_enabled"] is True


def test_unproven_research_artifact_cannot_be_promoted(tmp_path, monkeypatch):
    _research_artifact(tmp_path, monkeypatch, promoted=False)

    with pytest.raises(ValueError, match="has not passed"):
        promote_execution_artifact(
            source_version="research-v1",
            new_version="execution-v1",
            confirmed=True,
        )
