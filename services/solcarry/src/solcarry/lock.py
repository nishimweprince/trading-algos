"""Pre-registration lock: hash of thresholds + variants + alpha + gating rule."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config, canonical_json, sha256_text


def preregistered_payload(cfg: Config) -> dict[str, Any]:
    return {
        "thresholds": cfg.thresholds.model_dump(mode="json"),
        "variants": list(cfg.variants),
        "alpha": list(cfg.windows.alpha),
        "gating": cfg.gating.model_dump(mode="json"),
    }


def preregistration_hash(cfg: Config) -> str:
    return sha256_text(canonical_json(preregistered_payload(cfg)))


def write_lock(cfg: Config, lock_file: Path) -> dict[str, Any]:
    rec = {
        "hash": preregistration_hash(cfg),
        "locked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "payload": preregistered_payload(cfg),
    }
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
    return rec


@dataclass(frozen=True)
class LockStatus:
    ok: bool
    state: str  # LOCKED | MISSING | MISMATCH
    detail: str
    locked_at: str | None = None


def check_lock(cfg: Config, lock_file: Path) -> LockStatus:
    if not lock_file.exists():
        return LockStatus(False, "MISSING", f"no lock file at {lock_file}")
    try:
        rec = json.loads(lock_file.read_text())
    except json.JSONDecodeError as e:
        return LockStatus(False, "MISMATCH", f"unreadable lock file: {e}")
    want = preregistration_hash(cfg)
    if rec.get("hash") != want:
        return LockStatus(
            False,
            "MISMATCH",
            "thresholds/variants/alpha/gating changed since lock "
            f"(lock {str(rec.get('hash'))[:12]} vs config {want[:12]})",
            rec.get("locked_at"),
        )
    return LockStatus(True, "LOCKED", f"hash {want[:12]}", rec.get("locked_at"))
