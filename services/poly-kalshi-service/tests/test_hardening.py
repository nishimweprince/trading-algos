"""Fixes from the first production export (2026-10-06)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import OPEN, PEM, at, make_engine, make_settings, setup_window, window
from fastapi.testclient import TestClient
from test_feeds import FakeConnect, _until
from test_status import _trade

from poly_kalshi_service.app import is_loopback
from poly_kalshi_service.feeds import KalshiFeed, KalshiSigner
from poly_kalshi_service.ledger import Ledger, Position
from poly_kalshi_service.status_app import create_status_app
from poly_kalshi_service.telegram_commands import CONFLICT_BACKOFF_SECONDS, TelegramCommands
from poly_kalshi_service.trades import in_epoch, read_journal

# --- 1. results epoch and build stamp -------------------------------------------------


def test_every_journal_record_carries_the_stamp(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    engine.stamp = {"epoch": "2", "build": "abc123", "config": "cfg"}
    w = window()
    setup_window(engine, w)
    body = {"type": "resolution", "venue": "kalshi", "asset": "BTC", "open_unix": OPEN}
    engine.handle_line(at(OPEN, 950), "c", json.dumps(body | {"outcome": "yes"}))
    records = list(read_journal(tmp_path / "journal"))
    assert records and all(r["epoch"] == "2" and r["build"] == "abc123" for r in records)


def test_in_epoch_keeps_one_epoch_and_counts_the_rest() -> None:
    records = [{"n": 0}, {"epoch": "1", "n": 1}, {"epoch": "2", "n": 2}, {"epoch": "2", "n": 3}]
    kept, excluded = in_epoch(records, "2")
    assert [r["n"] for r in kept] == [2, 3] and excluded == 2
    assert in_epoch(records, None)[0] == kept  # default: the latest epoch
    assert in_epoch([{"n": 0}], None) == ([{"n": 0}], 0)  # a replay has no epochs


def test_a_new_epoch_archives_the_ledger_and_starts_fresh(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    old = Ledger.open(path, 1000.0)  # a legacy ledger: no epoch
    old.buy(Position("BTC", OPEN, "yes", 100, 40.0, 1.68, 0))
    fresh = Ledger.open(path, 1000.0, epoch="2")
    assert fresh.cash == 1000.0 and not fresh.positions and fresh.epoch == "2"
    assert (tmp_path / "ledger.epoch-legacy.json").is_file()
    fresh.save()
    assert Ledger.open(path, 5.0, epoch="2").cash == 1000.0  # same epoch: kept


def test_status_page_shows_only_the_current_epoch(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, PK_RESULTS_EPOCH="2")
    journal = settings.state_dir / "journal"
    journal.mkdir(parents=True)
    legacy = _trade(OPEN, 0.4, True)
    for row in legacy:
        row.pop("epoch")
    current = [r | {"epoch": "2"} for r in _trade(OPEN + 900, 0.4, False)]
    (journal / "d.jsonl").write_text("\n".join(json.dumps(r) for r in legacy + current) + "\n")

    async def fetch() -> dict[str, Any]:
        return {"execution_mode": "paper", "epoch": "2", "build": "abc"}

    with TestClient(create_status_app(settings, fetch=fetch, poll=False)) as client:
        body = client.get("/api/summary").json()
    assert body["epoch"] == "2"
    assert body["excluded_records"] == 3
    assert body["results"]["quality"]["n"] == 1 and body["results"]["quality"]["wins"] == 0
    assert body["live"]["build"] == "abc"


# --- 2. Kalshi seq on every frame ---------------------------------------------------------


async def test_subscription_ack_with_a_seq_is_not_a_gap() -> None:
    emitted: list[tuple[str, str]] = []
    connect = FakeConnect()
    feed = KalshiFeed(
        "wss://k.test/trade-api/ws/v2",
        lambda v, t: emitted.append((v, t)),
        KalshiSigner("kid", PEM),
        ws_connect=connect,
        max_backoff=0.01,
    )
    task = asyncio.create_task(feed.run())
    await connect.opened.wait()
    socket = connect.sockets[0]
    delta = {"type": "orderbook_delta", "sid": 1, "msg": {}}
    for frame in (
        delta | {"seq": 1},
        {"type": "ok", "sid": 1, "seq": 2, "msg": {"market_tickers": ["T2"]}},  # add_markets ack
        delta | {"seq": 3},
    ):
        await socket.inbox.put(json.dumps(frame))
    await _until(lambda: len([e for e in emitted if e[0] == "k"]) == 3)
    assert feed.seq_gaps == 0
    assert len(connect.sockets) == 1  # no reconnect
    await socket.inbox.put(json.dumps(delta | {"seq": 5}))  # a real gap still resyncs
    await _until(lambda: feed.seq_gaps == 1)
    resync = [json.loads(t) for v, t in emitted if v == "c" and '"resync"' in t]
    assert resync[0]["frame"] == "orderbook_delta"
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# --- 3. throughput --------------------------------------------------------------------------


async def test_slow_consumer_closes_are_counted() -> None:
    @contextlib.asynccontextmanager
    async def refused(url: str, headers: Any = None):  # type: ignore[no-untyped-def]
        raise ConnectionError("received 1013 (try again later) slow consumer")
        yield

    feed = KalshiFeed(
        "wss://k.test/trade-api/ws/v2",
        lambda v, t: None,
        KalshiSigner("kid", PEM),
        ws_connect=refused,
        max_backoff=0.01,
    )
    task = asyncio.create_task(feed.run())
    await _until(lambda: feed.slow_consumer >= 2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert feed.stats()["slow_consumer"] >= 2


# --- 4. Telegram conflict ----------------------------------------------------------------


async def test_telegram_conflict_alerts_once_and_backs_off() -> None:
    import httpx
    from pydantic import SecretStr

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409, json={"ok": False, "description": "Conflict: terminated by other getUpdates"}
        )

    sleeps: list[float] = []
    alerts: list[str] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bot = TelegramCommands(
        SecretStr("123:abc"), frozenset({1}), lambda c, u: None,  # type: ignore[arg-type, return-value]
        http=http, sleep=sleep, on_conflict=alerts.append,
    )  # fmt: skip
    with pytest.raises(asyncio.CancelledError):
        await bot.run()
    await http.aclose()
    assert len(alerts) == 1
    assert sleeps == [CONFLICT_BACKOFF_SECONDS] * 3
    assert bot.stats()["conflicts"] == 3


# --- 5. exposure ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "loopback"),
    [("127.0.0.1", True), ("localhost", True), ("::1", True), ("0.0.0.0", False),
     ("20.223.238.225", False), ("pk.example", False)],
)  # fmt: skip
def test_is_loopback(host: str, loopback: bool) -> None:
    assert is_loopback(host) is loopback


def test_cpu_sample_is_a_percentage(tmp_path: Path) -> None:
    from poly_kalshi_service.alerts import Alerts
    from poly_kalshi_service.runtime import Runtime

    runtime = Runtime(
        make_settings(tmp_path), engine=make_engine(), discovery=None,  # type: ignore[arg-type]
        kalshi=None, poly=None, alerts=Alerts(None),
    )  # fmt: skip
    runtime._cpu_mark = (runtime._cpu_mark[0] - 1.0, runtime._cpu_mark[1])
    runtime._sample_cpu()
    host = runtime.host()
    assert host["cpu_pct"] is not None and 0 <= host["cpu_pct"] <= 100 * (host["cpus"] or 1)
