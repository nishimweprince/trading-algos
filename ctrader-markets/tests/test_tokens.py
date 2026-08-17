from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ctrader.tokens import TokenPair, TokenStore


def _store(tmp_path: Path, **fallback: str) -> TokenStore:
    return TokenStore(
        tmp_path / "token-cache.json",
        fallback=TokenPair(
            access_token=fallback.get("access_token", "env-access"),
            refresh_token=fallback.get("refresh_token", "env-refresh"),
        ),
    )


def test_falls_back_to_env_when_no_cache_exists(tmp_path: Path) -> None:
    assert _store(tmp_path).load().access_token == "env-access"
    cache = tmp_path / "token-cache.json"
    assert cache.is_file()
    assert stat.S_IMODE(os.stat(cache).st_mode) == 0o600


def test_cache_wins_over_env(tmp_path: Path) -> None:
    """The cache holds the rotated pair; .env holds the one that may be dead."""
    (tmp_path / "token-cache.json").write_text(
        json.dumps({"access_token": "cached-access", "refresh_token": "cached-refresh"})
    )

    pair = _store(tmp_path).load()

    assert (pair.access_token, pair.refresh_token) == ("cached-access", "cached-refresh")


def test_corrupt_cache_falls_back_rather_than_crashing(tmp_path: Path) -> None:
    (tmp_path / "token-cache.json").write_text("{not json")
    assert _store(tmp_path).load().access_token == "env-access"


def test_empty_cached_token_falls_back(tmp_path: Path) -> None:
    (tmp_path / "token-cache.json").write_text(json.dumps({"access_token": "  "}))
    assert _store(tmp_path).load().access_token == "env-access"


def test_record_refresh_persists_the_rotated_pair(tmp_path: Path) -> None:
    """The old refresh token dies on use. Losing the new one means redoing OAuth."""
    store = _store(tmp_path)
    store.load()

    store.record_refresh(access_token="new-access", refresh_token="new-refresh", expires_in=3600)

    reloaded = _store(tmp_path).load()
    assert (reloaded.access_token, reloaded.refresh_token) == ("new-access", "new-refresh")
    assert reloaded.expires_at is not None


def test_cache_file_is_owner_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_refresh(access_token="a", refresh_token="b", expires_in=60)

    mode = stat.S_IMODE(os.stat(tmp_path / "token-cache.json").st_mode)

    assert mode == 0o600


def test_save_leaves_no_temp_files_behind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_refresh(access_token="a", refresh_token="b", expires_in=60)

    assert [p.name for p in tmp_path.iterdir()] == ["token-cache.json"]


def test_a_new_store_trusts_its_token(tmp_path: Path) -> None:
    assert _store(tmp_path).is_invalidated is False


def test_invalidate_marks_the_token_untrusted(tmp_path: Path) -> None:
    """Revocation is invisible in the pair itself: a token can be well inside its
    lifetime and still be dead, so the flag is the only record of it."""
    store = _store(tmp_path)
    store.load()

    store.invalidate()

    assert store.is_invalidated is True


def test_a_rotation_clears_the_invalidated_flag(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.invalidate()

    store.record_refresh(access_token="fresh", refresh_token="fresh-refresh", expires_in=60)

    assert store.is_invalidated is False


def test_parent_directory_is_created(tmp_path: Path) -> None:
    store = TokenStore(
        tmp_path / "nested" / "deeper" / "token-cache.json",
        fallback=TokenPair(access_token="a"),
    )
    store.record_refresh(access_token="a", refresh_token="b", expires_in=60)

    assert (tmp_path / "nested" / "deeper" / "token-cache.json").is_file()


def test_refresh_is_scheduled_before_expiry() -> None:
    pair = TokenPair(
        access_token="a",
        refresh_token="b",
        expires_at=datetime.now(UTC) + timedelta(seconds=1000),
    )

    seconds = pair.seconds_until_refresh()

    assert seconds is not None
    assert 750 < seconds < 850  # 80% of the remaining lifetime


def test_no_known_expiry_means_no_proactive_refresh() -> None:
    """A token loaded from .env has no stated lifetime; it is handled reactively."""
    assert TokenPair(access_token="a").seconds_until_refresh() is None


def test_already_expired_token_refreshes_immediately() -> None:
    pair = TokenPair(access_token="a", expires_at=datetime.now(UTC) - timedelta(seconds=10))
    assert pair.seconds_until_refresh() == 0.0
