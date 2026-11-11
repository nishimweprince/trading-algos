"""Raw cache: untouched payloads + manifest.json per source, sha256, idempotent fetch."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class RawCache:
    """One cache per source: ``<raw>/<source>/<relpath>`` plus ``manifest.json``.

    The manifest maps relpath -> latest record and keeps an append-only ``log``
    so refetches stay auditable.
    """

    def __init__(self, raw_root: Path, source: str) -> None:
        self.dir = raw_root / source
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.dir / "manifest.json"
        self._lock = threading.Lock()
        self._manifest: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if self.manifest_path.exists():
            data: dict[str, Any] = json.loads(self.manifest_path.read_text())
            data.setdefault("files", {})
            data.setdefault("gaps", {})
            data.setdefault("log", [])
            return data
        return {"files": {}, "gaps": {}, "log": []}

    def _save(self) -> None:
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._manifest, indent=1, sort_keys=True))
        tmp.replace(self.manifest_path)

    def path(self, relpath: str) -> Path:
        return self.dir / relpath

    def has(self, relpath: str) -> bool:
        """True if the file exists and matches the recorded sha256."""
        rec = self._manifest["files"].get(relpath)
        p = self.path(relpath)
        if rec is None or not p.exists():
            return False
        return bool(sha256_bytes(p.read_bytes()) == rec.get("sha256"))

    def is_gap(self, relpath: str) -> bool:
        return relpath in self._manifest["gaps"]

    def record(self, relpath: str) -> dict[str, Any] | None:
        rec = self._manifest["files"].get(relpath)
        return dict(rec) if rec is not None else None

    def put(
        self,
        relpath: str,
        payload: bytes,
        url: str,
        http_status: int,
        rows: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Path:
        p = self.path(relpath)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(payload)
        rec: dict[str, Any] = {
            "url": url,
            "fetched_at": utcnow_iso(),
            "http_status": http_status,
            "bytes": len(payload),
            "sha256": sha256_bytes(payload),
            "rows": rows,
        }
        if extra:
            rec.update(extra)
        with self._lock:
            self._manifest["files"][relpath] = rec
            self._manifest["gaps"].pop(relpath, None)
            self._manifest["log"].append({"relpath": relpath, **rec})
            self._save()
        return p

    def gap(self, relpath: str, url: str, http_status: int, note: str = "") -> None:
        rec = {"url": url, "fetched_at": utcnow_iso(), "http_status": http_status, "note": note}
        with self._lock:
            self._manifest["gaps"][relpath] = rec
            self._manifest["log"].append({"relpath": relpath, "gap": True, **rec})
            self._save()

    def files(self) -> dict[str, dict[str, Any]]:
        return dict(self._manifest["files"])

    def gaps(self) -> dict[str, dict[str, Any]]:
        return dict(self._manifest["gaps"])

    def meta_get(self, key: str) -> Any:
        return self._manifest.get("meta", {}).get(key)

    def meta_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._manifest.setdefault("meta", {})[key] = value
            self._save()
