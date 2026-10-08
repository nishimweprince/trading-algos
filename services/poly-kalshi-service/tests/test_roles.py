"""The trade path and the statistics, split across two processes, write the same journal.

``Engine(role="trade")`` runs in the trader, ``Engine(role="observe")`` in the
observer, and ``role="full"`` is the reference: on the same lines the two halves
must produce exactly the full engine's records.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    OPEN,
    at,
    ctl,
    kalshi_delta,
    kalshi_snapshot,
    make_engine,
    poly_book,
    poly_change,
    window,
)
from test_feeds import FakeConnect

from poly_kalshi_service.book import Quote
from poly_kalshi_service.decision import Action, StrategyParams, decide
from poly_kalshi_service.engine import Engine
from poly_kalshi_service.feeds import PolyBoltFeed
from poly_kalshi_service.observer import Observer, ObserverLink
from poly_kalshi_service.shadow import Shadow, parse_variants
from poly_kalshi_service.trades import JsonlDaily, read_journal

P10 = StrategyParams(gap_threshold=0.10)
VARIANTS = parse_variants(
    json.dumps(
        [
            {"name": "t20", "gap": 0.20},
            {"name": "cap40", "cap": 0.40},
            {"name": "plag250", "max_poly_lag_ms": 250},
            {"name": "pspread01", "max_poly_spread": 0.01},
        ]
    ),
    P10,
)


class ListJournal:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, t_ns: int, record: dict[str, Any]) -> None:
        self.records.append(record)


def _lines() -> list[tuple[int, str, str]]:
    """A window with a crossing, a fill, the market moving, a gap close, the roll
    and Polymarket's resolution: every kind of record."""
    w = window()
    t0 = at(OPEN, 0.5)
    t = at(OPEN, 20)
    nxt = OPEN + 900
    out = [
        (t0, "c", ctl(w.control())),
        (t0 + 1, "k", kalshi_snapshot(w.kalshi_ticker, [(0.70, 500)], [(0.28, 500)])),
        (t0 + 2, "p", poly_book(w.yes_token, [(0.44, 300)], [(0.46, 60), (0.47, 80)])),
        (t0 + 3, "p", poly_book(w.no_token, [(0.54, 300)], [(0.56, 300)])),
        (t, "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1)),
        (t + 200_000_000, "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1)),
        (t + 900_000_000, "p", poly_book(w.yes_token, [(0.60, 30)], [(0.62, 30)])),
        (t + 1_300_000_000, "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1)),
        (t + 3_000_000_000, "k", kalshi_delta(w.kalshi_ticker, "yes", 0.62, 400)),
        (t + 3_100_000_000, "k", kalshi_delta(w.kalshi_ticker, "no", 0.36, 400)),
        (t + 3_250_000_000, "p", poly_book(w.no_token, [(0.38, 10)], [(0.40, 10)])),
        (t + 4_000_000_000, "p", poly_change(w.yes_token, 0.61, 5, "BUY", timestamp="1")),
    ]
    for dt in (5.3, 30.3, 60.3, 120.3):
        out.append((t + int(dt * 1e9), "p", poly_book(w.yes_token, [(0.55, 5)], [(0.57, 5)])))
    out.append((at(nxt, 1), "c", ctl(window(open_unix=nxt).control())))
    resolution = {
        "type": "resolution", "venue": "poly", "asset": "BTC", "open_unix": OPEN, "outcome": "yes",
    }  # fmt: skip
    out.append((at(nxt, 300), "c", ctl(resolution)))
    out.append((at(nxt, 301), "c", ctl({**resolution, "venue": "kalshi"})))
    return out


def _full(tmp_path: Path) -> list[dict[str, Any]]:
    engine = make_engine(tmp_path, params=P10)
    engine.shadow = Shadow(VARIANTS, P10)
    engine.journal = ListJournal()  # type: ignore[assignment]
    for line in _lines():
        engine.handle_line(*line)
    return engine.journal.records  # type: ignore[attr-defined]


def _split(tmp_path: Path) -> tuple[list[dict[str, Any]], Engine, Engine]:
    """The trader's lines and records go through the observer in pipe order."""
    trader = make_engine(tmp_path / "t", params=P10)
    trader.role, trader.observe = "trade", False
    pipe: list[str] = []

    class Sink:
        def write(self, t_ns: int, record: dict[str, Any]) -> None:
            pipe.append(f"{t_ns} x {json.dumps({'k': 'j', 'r': record})}")

    trader.journal = Sink()  # type: ignore[assignment]
    obs_engine = make_engine(None, params=P10, paper=False)
    obs_engine.role, obs_engine.trade = "observe", False
    obs_engine.shadow = Shadow(VARIANTS, P10)
    journal = ListJournal()
    obs_engine.journal = journal  # type: ignore[assignment]
    observer = Observer(obs_engine, journal)  # type: ignore[arg-type]
    for recv_ns, venue, text in _lines():
        pipe.append(f"{recv_ns} {venue} {text}")  # the line goes out first
        trader.handle_line(recv_ns, venue, text)  # its records follow it
        for line in pipe:
            observer.handle(line)
        pipe.clear()
    return journal.records, trader, obs_engine


def test_trade_and_observe_write_exactly_the_full_journal(tmp_path: Path) -> None:
    full = _full(tmp_path)
    split, _, _ = _split(tmp_path)
    kinds = {r["type"] for r in full}
    assert {"decision", "fill", "markout", "window", "shadow", "shadow_settle", "settle"} <= kinds
    assert {"resolution"} <= kinds
    key = lambda r: json.dumps(r, sort_keys=True)  # noqa: E731
    assert sorted(map(key, split)) == sorted(map(key, full))
    # Same order too: the observer writes in the trader's line order.
    assert [r["type"] for r in split] == [r["type"] for r in full]


def test_the_trader_runs_no_statistics(tmp_path: Path) -> None:
    _, trader, obs = _split(tmp_path)
    assert not trader.series["BTC"] and not trader.markouts
    assert "feed_ms_p" not in trader.metrics.snapshot()
    assert not any(k.startswith(("bbo_", "invalid_")) for k in trader.counters)
    assert [p.status for p in trader.ledger.positions.values()] == ["settled"]
    assert obs.series["BTC"] and obs.ledger.positions == {}  # the observer never trades
    assert obs.counters.get("fills", 0) == 0


def test_engine_rejects_an_unknown_role() -> None:
    with pytest.raises(ValueError, match="role"):
        Engine(P10, ("BTC",), ledger=None, risk=None, venue=None, role="stats")  # type: ignore[arg-type]


# --- the decision filters added with the split -----------------------------------------


def test_poly_lag_filter_waits_and_reads_the_cheap_leg() -> None:
    k, y, n = Quote(True, 0.70, 0.72), Quote(True, 0.44, 0.46), Quote(True, 0.54, 0.56)
    params = StrategyParams(gap_threshold=0.10, max_poly_lag_ms=250)
    assert decide(k, y, n, params, 100, False).action is Action.ENTER  # no lags passed
    late = decide(k, y, n, params, 100, False, poly_lag_ms=(400.0, 10.0))
    assert (late.action, late.reason) == (Action.NONE, "poly_lag")
    # Buying YES: the NO book's lag does not matter.
    assert decide(k, y, n, params, 100, False, poly_lag_ms=(10.0, 900.0)).action is Action.ENTER


def test_size_to_depth_orders_only_what_is_offered(tmp_path: Path) -> None:
    from conftest import setup_window

    engine = make_engine(tmp_path, params=P10)
    engine.size_to_depth = True
    w = window()
    setup_window(engine, w)  # YES ask 0.46 x 60, 0.47 x 80
    engine.handle_line(at(OPEN, 20), "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1))
    (decision,) = [r for r in read_journal(tmp_path / "journal") if r["type"] == "decision"]
    assert decision["contracts"] == 60  # not floor(50 / 0.46) = 108


# --- the pipe ------------------------------------------------------------------------


def _cat_to(path: Path) -> list[str]:
    code = "import sys; open(sys.argv[1], 'wb').write(sys.stdin.buffer.read())"
    return [sys.executable, "-c", code, str(path)]


async def test_link_sends_lines_and_records_in_order_and_closes_with_eof(tmp_path: Path) -> None:
    out = tmp_path / "pipe.txt"
    link = ObserverLink(_cat_to(out), fallback=JsonlDaily(tmp_path / "fb"))
    await link.start()
    link.line(1, "p", '{"a": 1}')
    link.journal(2, {"type": "decision", "asset": "BTC"})
    link.line(3, "k", "{}")
    await asyncio.sleep(0)  # one loop turn: one write
    assert link.lines_sent == 3
    await link.close()
    lines = out.read_text().splitlines()
    assert lines[0] == '1 p {"a": 1}' and lines[2] == "3 k {}"
    t_ns, venue, body = lines[1].split(" ", 2)
    assert (t_ns, venue, json.loads(body)) == (
        "2",
        "x",
        {"k": "j", "r": {"type": "decision", "asset": "BTC"}},
    )


async def test_link_falls_back_to_disk_and_respawns_when_the_child_dies(tmp_path: Path) -> None:
    downs: list[str] = []
    link = ObserverLink(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        fallback=JsonlDaily(tmp_path / "fb"),
        on_down=downs.append,
    )
    await link.start()
    assert link.proc is not None
    link.proc.kill()
    await link.proc.wait()
    link.journal(1_791_297_900 * 10**9, {"type": "fill", "asset": "BTC"})
    link.line(5, "p", "{}")
    await asyncio.sleep(0)
    assert link.fallback_writes == 1 and link.dropped == 1
    assert [r["type"] for r in read_journal(tmp_path / "fb")] == ["fill"]
    await link.check()
    assert link.alive and link.spawns == 2 and downs
    link.line(6, "p", "{}")
    await asyncio.sleep(0)
    assert link.lines_sent == 2  # the gap note goes out ahead of the first line
    await link.close()


async def test_link_drops_market_lines_but_never_records_when_behind(tmp_path: Path) -> None:
    out = tmp_path / "pipe.txt"
    link = ObserverLink(_cat_to(out), fallback=JsonlDaily(tmp_path / "fb"), max_buffer_bytes=-1)
    await link.start()
    link.line(1, "p", "{}")
    link.journal(2, {"type": "settle"})
    await asyncio.sleep(0)
    assert link.dropped == 1 and link.lines_sent == 1
    await link.close()
    (line,) = out.read_text().splitlines()
    assert line.split(" ", 2)[1] == "x"


def test_observer_counts_bad_lines_and_notes_gaps() -> None:
    engine = make_engine(None, params=P10, paper=False)
    engine.role, engine.trade = "observe", False

    class Rec:
        gaps = 0
        lines: list[tuple[str, int, str]] = []

        def write(self, venue: str, recv_ns: int, text: str) -> None:
            self.lines.append((venue, recv_ns, text))

        def note_gap(self) -> None:
            self.gaps += 1

        def stats(self) -> dict[str, Any]:
            return {"enabled": True}

    rec = Rec()
    observer = Observer(engine, ListJournal(), rec)  # type: ignore[arg-type]
    observer.handle("garbage")
    observer.handle('5 x {"k": "gap", "lines": 3}')
    observer.handle("7 b {}")
    assert observer.bad_lines == 1 and rec.gaps == 1 and rec.lines == [("b", 7, "{}")]
    status = observer.status()
    assert status["lines"] == 1 and status["engine"]["counters"]["unknown_venue"] == 1


# --- PolyBolt -------------------------------------------------------------------------


async def test_polybolt_authenticates_then_subscribes_and_never_writes_control_lines() -> None:
    connect = FakeConnect()
    emitted: list[tuple[str, str]] = []
    feed = PolyBoltFeed(
        "wss://x", lambda v, t: emitted.append((v, t)), ("k", "s", "p"), ("BTC", "SOL"),
        ws_connect=connect,
    )  # fmt: skip
    task = asyncio.create_task(feed.run())
    await connect.opened.wait()
    await asyncio.sleep(0)
    sock = connect.sockets[0]
    assert sock.sent[0]["op"] == "auth" and sock.sent[0]["auth"]["apiKey"] == "k"
    assert len(sock.sent) == 1  # nothing before the auth is acknowledged
    await sock.inbox.put('{"op": "auth", "rid": "auth", "ok": true}')
    await asyncio.sleep(0.01)
    sub = sock.sent[1]
    assert sub["op"] == "subscribe"
    assert {"channel": "price.crypto", "filter": {"symbol": "btcusd"}} in sub["subscriptions"]
    twap = {"channel": "price.crypto.twap", "filter": {"symbol": "solusd", "window_seconds": 60}}
    assert twap in sub["subscriptions"]
    await sock.inbox.put('{"channel": "price.crypto", "seq": 1, "value": 1}')
    await asyncio.sleep(0.01)
    assert {v for v, _ in emitted} == {"b"}  # the reconnect note too
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_polybolt_auth_failure_reconnects() -> None:
    connect = FakeConnect()
    feed = PolyBoltFeed("wss://x", lambda v, t: None, ("k", "s", "p"), ("BTC",), ws_connect=connect)
    task = asyncio.create_task(feed.run())
    await connect.opened.wait()
    await connect.sockets[0].inbox.put('{"op": "auth", "ok": false, "error": "bad key"}')
    await asyncio.sleep(0.01)
    assert feed.auth_failures == 1 and not feed.connected
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_engine_ignores_polybolt_lines_and_their_reconnects() -> None:
    from conftest import setup_window

    engine = make_engine(None, params=P10)
    w = window()
    setup_window(engine, w)
    engine.handle_line(at(OPEN, 2), "b", '{"type": "pk_reconnect", "venue": "b"}')
    engine.handle_line(at(OPEN, 2) + 1, "c", '{"type": "reconnect", "venue": "b"}')
    assert engine.quotes("BTC") is not None and all(q.valid for q in engine.quotes("BTC") or ())


# --- the real observer process ---------------------------------------------------------


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_observer_process_records_the_tape_and_writes_the_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gzip

    import httpx
    from conftest import API_KEY, PEM

    from poly_kalshi_service.observer import observer_argv

    key = tmp_path / "kalshi.pem"
    key.write_bytes(PEM)
    port = _free_port()
    (tmp_path / ".env.e2e").write_text(
        "\n".join(
            [
                f"API_KEY={API_KEY}",
                "KALSHI_API_KEY_ID=test-key-id",
                f"KALSHI_PRIVATE_KEY_PATH={key}",
                "PK_MAX_OPEN_NOTIONAL_USD=500",
                "PK_DAILY_LOSS_LIMIT_USD=250",
                "PK_ASSETS=BTC",
                "PK_GAP_THRESHOLD=0.10",
                f"PK_RECORD_DIR={tmp_path / 'raw'}",
                f"PK_STATE_DIR={tmp_path / 'state'}",
                f"EVENTS_LOG_PATH={tmp_path / 'events.jsonl'}",
                f"PK_OBSERVER_PORT={port}",
                "PK_HOST_TAG=test",
            ]
        )
    )
    monkeypatch.chdir(tmp_path)
    link = ObserverLink(observer_argv("e2e"), fallback=JsonlDaily(tmp_path / "fb"))
    await link.start()
    lines = _lines()
    for recv_ns, venue, text in lines[:6]:
        link.line(recv_ns, venue, text)
    link.journal(lines[4][0], {"type": "decision", "asset": "BTC", "open_unix": OPEN})
    await asyncio.sleep(0)
    status = None
    async with httpx.AsyncClient() as http:
        for _ in range(100):  # the child imports, loads settings, binds
            try:
                r = await http.get(
                    f"http://127.0.0.1:{port}/v1/obs/status", headers={"X-API-Key": API_KEY}
                )
                status = r.json()
                if status["lines"] >= 6:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    await link.close()
    assert link.proc is None and link.fallback_writes == 0
    assert status is not None and status["lines"] == 6 and status["journal_records"] == 1
    tapes = sorted((tmp_path / "raw").rglob("*.gz"))
    assert tapes and tapes[0].with_name(tapes[0].name + ".json").is_file()  # manifest on EOF
    with gzip.open(tapes[0], "rt") as handle:
        taped = [line.split(" ", 2)[1] for line in handle]
    assert taped == [v for _, v, _ in lines[:6]]  # no ``x`` line on the tape
    records = list(read_journal(tmp_path / "state" / "journal"))
    assert [r["type"] for r in records] == ["decision"]
