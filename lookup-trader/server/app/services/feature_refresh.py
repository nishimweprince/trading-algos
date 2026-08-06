"""Incremental feature refresh entry point shared by Capital CLIs and daemon."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]


def refresh_h1_features(first_changed: datetime) -> None:
    """Rebuild changed rows plus their 48-bar forward-label dependency window."""
    if first_changed.tzinfo is None:
        first_changed = first_changed.replace(tzinfo=UTC)
    since = first_changed.astimezone(UTC) - timedelta(hours=48)
    subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "scripts" / "build_bar_features.py"),
            "--symbol",
            "XAUUSD",
            "--timeframe",
            "H1",
            "--from",
            since.isoformat(),
            "--rebuild",
        ],
        cwd=_REPO_ROOT,
        check=True,
    )
