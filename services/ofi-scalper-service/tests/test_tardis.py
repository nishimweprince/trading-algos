"""Tardis.dev downloads: retries, and raw frames converted into replayable recordings."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from ta_plugin_binance_futures.testing import agg_trade_frame, depth_frame

from research.collect import tardis, tardis_feed
from research.collect.tardis_feed import convert_day, parse_iso_ns
from research.replay import find_session, replay_files

DAY = date(2026, 9, 1)


def test_parse_iso_ns_keeps_tardis_100ns_digits() -> None:
    base = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp()) * 1_000_000_000
    assert parse_iso_ns("2026-09-01T00:00:00.0259231Z") == base + 25_923_100
    assert parse_iso_ns("2026-09-01T00:00:01Z") == base + 1_000_000_000


def test_download_retries_server_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tardis, "_RETRY_BACKOFF_SECONDS", (0, 0))
    body = gzip.compress(b"exchange,symbol\n")
    calls = {"ok": 0, "denied": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "ETHUSDT" in request.url.path:
            calls["denied"] += 1
            return httpx.Response(403)
        calls["ok"] += 1
        return httpx.Response(500) if calls["ok"] == 1 else httpx.Response(200, content=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        path = tardis.download(client, "trades", "BTCUSDT", DAY, tmp_path)
        denied = tardis.download(client, "trades", "ETHUSDT", DAY, tmp_path)
    assert path is not None and path.read_bytes() == body and calls["ok"] == 2
    assert denied is None and calls["denied"] == 1
    assert not list(tmp_path.rglob("*.part"))


def _iso(second: float) -> str:
    return f"2026-09-01T00:00:{second:010.7f}Z"


def _snapshot(last_update_id: int, bid: str, ask: str) -> str:
    data = {"lastUpdateId": last_update_id, "bids": [[bid, "2.0"]], "asks": [[ask, "3.0"]]}
    frame = {"stream": "btcusdt@depthSnapshot", "generated": True, "data": data}
    return json.dumps(frame, separators=(",", ":"))  # compact, as Tardis serves it


def _minutes() -> dict[int, list[str]]:
    """Tardis's day start: a diff before the REST snapshot, which it then bridges."""
    first = [
        f"{_iso(0.01)} {depth_frame('BTCUSDT', 90, 100, 89, bids=[('60000.0', '1.0')])}",
        f"{_iso(0.02)} {_snapshot(95, '60000.0', '60000.1')}",
    ]
    second, prev = [], 100
    for i in range(30):
        final = prev + 5
        # Odd diffs pull the 60000.0 bid, even ones restore it: the touch alternates.
        bids = [("60000.0", "0"), ("59999.9", "1.0")] if i % 2 else [("60000.0", "1.5")]
        diff = depth_frame("BTCUSDT", prev + 1, final, prev, bids=bids)
        second.append(f"{_iso(1 + i * 0.2)} {diff}")
        prev = final
    second.append(f"{_iso(7.5)} {agg_trade_frame('BTCUSDT', 1, '60000.1', '0.5', False)}")
    return {0: first, 1: second}


def test_convert_day_writes_replayable_recordings(tmp_path: Path) -> None:
    minutes = _minutes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/data-feeds/binance-futures")
        channels = {f["channel"] for f in json.loads(request.url.params["filters"])}
        assert channels == set(tardis_feed.CHANNELS)
        lines = minutes.get(int(request.url.params["offset"]), [])
        return httpx.Response(200, text="\n".join(lines))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        summary = convert_day(client, DAY, ["BTCUSDT"], tmp_path, workers=2, minutes=3)
    assert summary["lines"] == 1 + 2 + 31 and summary["out_of_order"] == 0
    assert not (tmp_path / ".partial").exists()

    hour = tmp_path / "BTCUSDT" / "20260901" / "BTCUSDT_20260901_00.gz"
    manifest = json.loads(hour.with_name(hour.name + ".json").read_text())
    assert manifest["host"] == "tardis" and manifest["streams"]["depthSnapshot"] == 1

    session = find_session(tmp_path, datetime(2026, 9, 1, 23, tzinfo=UTC))
    assert session is not None and session["book_mode"] == "diff"
    files = sorted(tmp_path.glob("*/20260901/*.gz"))
    rows, replayer = replay_files(files, session=session, burst_trades=2)
    grid = [r for r in rows if r["trigger"] == "grid"]
    assert grid and all(r["best_ask"] == 60000.1 for r in grid)
    assert {r["best_bid"] for r in grid} == {60000.0, 59999.9}
    assert replayer.streams.syncs["BTCUSDT"].gaps == 0
