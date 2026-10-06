from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest
from conftest import (
    OPEN,
    at,
    kalshi_delta,
    kalshi_snapshot,
    make_engine,
    make_settings,
    poly_book,
    poly_change,
    window,
)

from poly_kalshi_service.alerts import Alerts
from poly_kalshi_service.decision import StrategyParams
from poly_kalshi_service.gates import evaluate
from poly_kalshi_service.recorder import Recorder
from poly_kalshi_service.recordings import iter_files, read_lines
from poly_kalshi_service.runtime import Runtime
from poly_kalshi_service.trades import read_journal
from research.nulls import arms, summarise
from research.replay import compare, replay


def _session() -> Iterator[tuple[int, str, str]]:
    """Two assets over two windows: an entry, a skip, a quiet window, resolutions."""
    btc, eth = window("BTC"), window("ETH")
    btc2, eth2 = window("BTC", OPEN + 900), window("ETH", OPEN + 900)
    for w in (btc, eth):
        t = at(OPEN, 0.5)
        yield t, "c", json.dumps(w.control())
        yield t, "k", kalshi_snapshot(w.kalshi_ticker, [(0.70, 500)], [(0.28, 500)])
        yield t, "p", poly_book(w.yes_token, [(0.44, 300)], [(0.46, 60)])
    # ETH's NO book is pricey and Kalshi is low: a skip.
    yield at(OPEN, 0.6), "p", poly_book(btc.no_token, [(0.54, 300)], [(0.56, 300)])
    yield at(OPEN, 0.6), "p", poly_book(eth.no_token, [(0.30, 300)], [(0.70, 300)])
    yield at(OPEN, 0.7), "k", kalshi_snapshot(eth.kalshi_ticker, [(0.10, 5)], [(0.88, 5)], seq=2)
    for sec in range(21, 200, 7):
        yield at(OPEN, sec), "p", poly_change(btc.yes_token, 0.43, sec, "BUY")
        yield at(OPEN, sec + 0.5), "p", poly_change(eth.yes_token, 0.43, sec, "BUY")
    yield at(OPEN, 880), "c", json.dumps(btc2.control())
    yield at(OPEN, 880), "c", json.dumps(eth2.control())
    for w in (btc2, eth2):
        yield at(OPEN, 890), "k", kalshi_snapshot(w.kalshi_ticker, [(0.48, 5)], [(0.50, 5)], seq=3)
        yield at(OPEN, 891), "p", poly_book(w.yes_token, [(0.47, 300)], [(0.49, 60)])
        yield at(OPEN, 891), "p", poly_book(w.no_token, [(0.51, 300)], [(0.53, 300)])
    for sec in range(901, 1500, 5):
        yield at(OPEN, sec), "k", kalshi_delta(btc2.kalshi_ticker, "no", 0.05, 1, seq=9)
    for asset, venue, outcome in (
        ("BTC", "poly", "yes"),
        ("BTC", "kalshi", "yes"),
        ("ETH", "poly", "no"),
        ("ETH", "kalshi", "yes"),
    ):
        body = {"type": "resolution", "venue": venue, "asset": asset, "open_unix": OPEN}
        yield at(OPEN, 1000), "c", json.dumps(body | {"outcome": outcome})


async def test_replay_of_the_tape_reproduces_the_live_run(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, PK_ASSETS="BTC,ETH")
    engine = make_engine(tmp_path / "live", assets=("BTC", "ETH"))
    recorder = Recorder(tmp_path / "raw", host_tag="test")
    script = list(_session())
    clock = iter(t for t, _, _ in script)
    runtime = Runtime(
        settings,
        engine=engine,
        discovery=None,  # type: ignore[arg-type]
        kalshi=None,
        poly=None,
        alerts=Alerts(None),
        recorder=recorder,
        clock_ns=lambda: next(clock),
    )
    recorder.start()
    for _t, venue, text in script:
        runtime.ingest(venue, text)
    recorder.stop()

    live = list(read_journal(tmp_path / "live" / "journal"))
    assert {r["action"] for r in live if r["type"] == "decision"} == {"enter", "skip"}
    assert [r["type"] for r in live].count("settle") == 1

    stats: dict[str, int] = {}
    files = iter_files(tmp_path / "raw")
    assert len(files) == 2  # the session crosses an hour: read in order across files
    replayed_engine, sampler = replay(
        read_lines(files, stats),
        StrategyParams(),
        ("BTC", "ETH"),
        journal_dir=tmp_path / "replay" / "journal",
    )
    assert stats["out_of_order"] == 0
    assert stats["lines"] == len(script)
    replayed = list(read_journal(tmp_path / "replay" / "journal"))
    result = compare(live, replayed)
    assert result["agreement"] == 1.0
    assert result["common"] == 2
    strip = lambda rs: [{k: v for k, v in r.items() if k != "cash"} for r in rs]  # noqa: E731
    assert strip(r for r in replayed if r["type"] in ("fill", "settle")) == strip(
        r for r in live if r["type"] in ("fill", "settle")
    )
    assert sampler is not None and sampler.rows
    assert all(0 <= row[2] < 900 for row in sampler.rows)


def test_reader_reports_a_silent_empty_tape(tmp_path: Path) -> None:
    import gzip

    path = tmp_path / "20261006" / "pk_20261006_14.gz"
    path.parent.mkdir()
    with gzip.open(path, "wt") as handle:
        handle.write("not a recording line\n")
    with pytest.raises(ValueError, match="reader bug"):
        list(read_lines(iter_files(tmp_path)))


def _journal(rows: list[tuple[str, int, float, str, str]]) -> list[dict]:
    """(asset, open_unix, vwap, side, poly outcome) -> fill, settle and resolution records."""
    out = []
    for asset, open_unix, vwap, side, outcome in rows:
        shares = 100.0
        fee = 0.07 * vwap * (1 - vwap) * shares
        payout = shares if side == outcome else 0.0
        out += [
            {"type": "decision", "asset": asset, "open_unix": open_unix, "action": "enter",
             "side": side, "entry_px": vwap, "blocked": None},
            {"type": "fill", "asset": asset, "open_unix": open_unix, "shares": shares,
             "ordered": shares, "vwap": vwap, "cost": vwap * shares, "fee": fee,
             "touch_px": vwap},
            {"type": "settle", "asset": asset, "open_unix": open_unix, "payout": payout,
             "pnl": payout - vwap * shares - fee},
            {"type": "resolution", "asset": asset, "open_unix": open_unix, "venue": "poly",
             "outcome": outcome},
            {"type": "resolution", "asset": asset, "open_unix": open_unix, "venue": "kalshi",
             "outcome": outcome if open_unix % 1800 else ("no" if outcome == "yes" else "yes")},
        ]  # fmt: skip
    return out


def test_gates_from_the_journal() -> None:
    day = 86_400
    rows = [("BTC", OPEN + d * day + 900 * i, 0.45, "yes", "yes" if i % 3 else "no")
            for d in range(3) for i in range(6)]  # fmt: skip
    result = evaluate(_journal(rows))
    assert result["fills"] == 18
    assert result["vwap"]["avg"] == pytest.approx(0.45)
    assert result["gates"]["fill_price"] is True
    assert result["gates"]["day_t"] is None  # n < 300: pending, never a pass
    assert result["bands"]["[0.4,0.5)"]["n"] == 18
    # 2 wins in 3 at 0.45: (0.667 - 0.45 - 0.0173) * 100 per share.
    assert result["net_cents_per_share"] == pytest.approx(100 * (2 / 3 - 0.45 - 0.017325), abs=0.01)
    assert result["days"] == 3
    assert 0 < result["venue_disagreement"] < 1


def test_null_battery_separates_signal_from_random_seconds() -> None:
    windows = [OPEN + 900 * i for i in range(40)]
    grid_rows = []
    journal = []
    for i, open_unix in enumerate(windows):
        outcome = "yes" if i % 5 else "no"  # 80% Up
        journal.append({"type": "resolution", "asset": "BTC", "open_unix": open_unix,
                        "venue": "poly", "outcome": outcome})  # fmt: skip
        journal.append({"type": "decision", "asset": "BTC", "open_unix": open_unix,
                        "action": "enter", "side": "yes", "entry_px": 0.50})  # fmt: skip
        for sec in range(0, 900, 30):
            # Polymarket fair at 0.80 most of the time; no gap to speak of.
            grid_rows.append(("BTC", open_unix, sec, 0.79, 0.81, 0.79, 0.81, 0.19, 0.21))
    grid = pd.DataFrame(
        grid_rows,
        columns=["asset", "open_unix", "sec", "k_bid", "k_ask", "y_bid", "y_ask", "n_bid", "n_ask"],
    )
    books = arms(grid, journal, cap=0.60)
    result = summarise(books)
    signal = result["arms"]["signal"]
    assert signal["n"] == 40
    assert signal["edge_pp"] == pytest.approx(30.0)  # 80% wins at 0.50
    null = result["arms"]["random_same_rule"]
    assert null["n"] == 40
    assert abs(null["edge_pp"]) < 2  # buying at the fair price earns nothing
    assert result["arms"]["fixed_cheap"]["n"] == 40
    assert result["bands_signal_vs_random_same_rule"]["[0.5,0.6)"]["n_signal"] == 40


def test_grid_seconds_are_window_relative(tmp_path: Path) -> None:
    lines = [(t, v, x) for t, v, x in _session() if t < at(OPEN, 100)]
    _, sampler = replay(iter(lines), StrategyParams(), ("BTC", "ETH"))
    assert sampler is not None
    secs = {row[2] for row in sampler.rows if row[0] == "BTC"}
    assert min(secs) >= 0 and max(secs) < 100
