"""OAuth token persistence.

The refresh token rotates on every use: ProtoOARefreshTokenRes returns a new one
and the old one dies immediately. If the rotated pair is not persisted, the next
restart authenticates with a dead token and the operator has to redo the browser
OAuth flow by hand. This file is the only writable state in the service.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Refresh at 80% of the token's life so a slow or failing refresh still has
# headroom before the broker starts rejecting the access token.
REFRESH_AT_FRACTION = 0.8


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None

    def refresh_due_at(self) -> datetime | None:
        if self.expires_at is None:
            return None
        remaining = self.expires_at - datetime.now(UTC)
        return datetime.now(UTC) + remaining * REFRESH_AT_FRACTION

    def seconds_until_refresh(self) -> float | None:
        due = self.refresh_due_at()
        if due is None:
            return None
        return max(0.0, (due - datetime.now(UTC)).total_seconds())


class TokenStore:
    """Reads the cache on start, writes atomically with 0600 on every rotation."""

    def __init__(self, path: Path, *, fallback: TokenPair) -> None:
        self._path = path
        self._fallback = fallback
        self._current = fallback
        self._invalidated = False

    @property
    def current(self) -> TokenPair:
        return self._current

    @property
    def is_invalidated(self) -> bool:
        """True when the broker told us the access token is dead out-of-band.

        Distinct from expiry: the token may be well inside its lifetime and
        still be revoked, so nothing in the pair itself reveals this.
        """
        return self._invalidated

    def invalidate(self) -> None:
        self._invalidated = True

    def load(self) -> TokenPair:
        """Prefer the cache over the .env values — it holds the rotated pair."""
        try:
            contents = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # Seed the single centrally owned cache before connecting. Besides
            # making its ownership and permissions explicit, this fails startup
            # immediately when production storage is not writable.
            self.save(self._fallback)
            return self._current
        except OSError:
            self._current = self._fallback
            return self._current

        try:
            raw = json.loads(contents)
        except json.JSONDecodeError:
            self._current = self._fallback
            return self._current

        access_token = str(raw.get("access_token") or "").strip()
        if not access_token:
            self._current = self._fallback
            return self._current

        expires_at = raw.get("expires_at")
        self._current = TokenPair(
            access_token=access_token,
            refresh_token=str(raw.get("refresh_token") or "").strip()
            or self._fallback.refresh_token,
            expires_at=datetime.fromisoformat(expires_at) if expires_at else None,
        )
        return self._current

    def save(self, pair: TokenPair) -> None:
        self._current = pair
        self._invalidated = False
        payload = {
            "access_token": pair.access_token,
            "refresh_token": pair.refresh_token,
            "expires_at": pair.expires_at.isoformat() if pair.expires_at else None,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Temp file in the destination directory so os.replace stays atomic.
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self._path.parent,
            prefix=self._path.name,
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(handle.name, 0o600)
            os.replace(handle.name, self._path)
        except BaseException:
            with_suppressed = Path(handle.name)
            if with_suppressed.exists():
                with_suppressed.unlink(missing_ok=True)
            raise

    def record_refresh(
        self, *, access_token: str, refresh_token: str, expires_in: int
    ) -> TokenPair:
        pair = TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=datetime.now(UTC) + timedelta(seconds=int(expires_in)),
        )
        self.save(pair)
        return pair
