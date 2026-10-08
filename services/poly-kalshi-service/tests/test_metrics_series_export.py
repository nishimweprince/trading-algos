from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    API_KEY,
    OPEN,
    at,
    kalshi_delta,
    make_engine,
    make_settings,
    poly_change,
    setup_window,
    window,
)
from fastapi.testclient import TestClient
from test_status import _trade

from poly_kalshi_service.metrics import Metrics
from poly_kalshi_service.results import paper_results
from poly_kalshi_service.status_app import create_status_app
from poly_kalshi_service.trades import read_journal


def test_metrics_percentiles_over_a_bounded_window() -> None:
    m = Metrics(window=100)
    for v in range(1000):
        m.add("x", float(v))
    snap = m.snapshot()["x"]
    assert snap["n"] == 1000 and snap["window"] == 100
    assert snap["p50"] == 950 and snap["max"] == 999


def test_engine_samples_one_point_per_second_and_times_legs(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    w = window()
    setup_window(engine, w)
    for sec in (5, 5.5, 6, 7, 20):
        change = poly_change(w.yes_token, 0.43, 10, "BUY")
        body = json.loads(change) | {"timestamp": str(at(OPEN, sec) // 1_000_000 - 40)}
        engine.handle_line(at(OPEN, sec), "p", json.dumps(body))
    points = engine.series_since(0)["BTC"]
    secs = [p[0] - OPEN for p in points]
    assert secs == [0, 5, 6, 7]  # one per second that saw an event, at its end
    assert points[-1][2] == pytest.approx(0.71) and points[-1][3] == pytest.approx(0.45)
    assert engine.series_since(OPEN + 6)["BTC"] == points[-1:]
    lat = engine.metrics.snapshot()
    assert lat["feed_ms_p"]["p50"] == pytest.approx(40, abs=1)
    (decision,) = [r for r in read_journal(tmp_path / "journal") if r["type"] == "decision"]
    assert decision["age_ms"]["kalshi"] == pytest.approx(19_500, abs=1)  # last Kalshi print 0.5s
    assert decision["age_ms"]["poly_yes"] == 0.0
    assert lat["age_ms_kalshi"]["n"] == 1


def test_resolution_delay_is_recorded(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    w = window()
    setup_window(engine, w)
    body = {"type": "resolution", "venue": "kalshi", "asset": "BTC", "open_unix": OPEN}
    engine.handle_line(at(OPEN, 907.5), "c", json.dumps(body | {"outcome": "yes"}))
    (res,) = [r for r in read_journal(tmp_path / "journal") if r["type"] == "resolution"]
    assert res["delay_s"] == 7.5
    assert engine.metrics.snapshot()["resolution_s_kalshi"]["p50"] == 7.5


def test_signal_check_compares_win_rate_with_kalshi_and_price() -> None:
    records: list[dict[str, Any]] = []
    for i in range(10):
        rows = _trade(OPEN + 900 * i, 0.40, win=i < 7)
        rows[0]["kalshi"] = {"bid": 0.69, "ask": 0.71}  # Kalshi says 70% for YES
        records += rows
    out = paper_results(records)
    assert out["signal"]["n"] == 10
    assert out["signal"]["win_rate"] == pytest.approx(0.7)
    assert out["signal"]["kalshi_implied"] == pytest.approx(0.70)
    assert out["signal"]["avg_price"] == pytest.approx(0.40)
    assert out["by_asset"]["BTC"]["n"] == 10
    assert out["by_sec"]["0-60s"]["n"] == 10  # sec_into 30
    assert out["execution"]["fill_rate"] == 1.0
    assert out["execution"]["avg_fill_ratio"] == pytest.approx(100 / 108)


def _status_app(tmp_path: Path, since_calls: list[int]) -> TestClient:
    settings = make_settings(tmp_path)
    journal = settings.state_dir / "journal"
    journal.mkdir(parents=True)
    (journal / "2026-10-06.jsonl").write_text(
        "\n".join(json.dumps(r) for r in _trade(OPEN, 0.4, True)) + "\n"
    )
    (settings.state_dir / "ledger.json").write_text('{"cash": 1}')
    settings.events_log_path.parent.mkdir(parents=True, exist_ok=True)
    settings.events_log_path.write_text('{"event":"heartbeat"}\n')
    raw = settings.record_dir / "20261006"
    raw.mkdir(parents=True)
    (raw / "pk_20261006_14_1.gz.json").write_text('{"lines": 5}')

    async def fetch() -> dict[str, Any]:
        return {
            "execution_mode": "paper",
            "params": {"gap_threshold": 0.2},
            "feeds": {"poly": {"messages": 100}},
            "engine": {"latency": {"feed_ms_p": {"p50": 12.0}}},
            "recent": [{"type": "decision", "asset": "BTC", "open_unix": OPEN, "sec_into": 30}],
        }

    async def fetch_series(since: int) -> dict[str, Any]:
        since_calls.append(since)
        pts = [[OPEN + s, OPEN, 0.7, 0.45, 0.44, 0.46, 0.54, 0.56] for s in range(3)]
        return {"points": {"BTC": [p for p in pts if p[0] > since]}}

    app = create_status_app(settings, fetch=fetch, fetch_series=fetch_series, poll=False)
    return TestClient(app)


def test_series_route_and_latency_pass_through(tmp_path: Path) -> None:
    calls: list[int] = []
    with _status_app(tmp_path, calls) as client:
        body = client.get("/api/series").json()
        later = client.get(f"/api/series?since={OPEN + 1}").json()
        summary = client.get("/api/summary").json()
    assert calls == [0]  # fetched once, then served from cache
    assert len(body["points"]["BTC"]) == 3
    assert [p[0] for p in later["points"]["BTC"]] == [OPEN + 2]
    assert body["threshold"] == 0.2
    assert body["decisions"][0]["sec_into"] == 30
    assert summary["live"]["latency"]["feed_ms_p"]["p50"] == 12.0
    assert summary["results"]["signal"]["n"] == 1


def test_export_is_one_zip_with_everything_and_no_secrets(tmp_path: Path) -> None:
    with _status_app(tmp_path, []) as client:
        response = client.get("/api/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "pk-export-test-" in response.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(response.content))
    names = set(zf.namelist())
    assert names == {
        "README.txt", "summary.json", "journal.jsonl", "events.jsonl", "ledger.json",
        "recordings.jsonl", "series.jsonl",
    }  # fmt: skip
    assert len(zf.read("journal.jsonl").decode().splitlines()) == 3
    assert json.loads(zf.read("events.jsonl").decode().splitlines()[0])["event"] == "heartbeat"
    assert json.loads(zf.read("recordings.jsonl"))["file"] == "20261006/pk_20261006_14_1.gz"
    series = [json.loads(x) for x in zf.read("series.jsonl").decode().splitlines()]
    assert series[0]["asset"] == "BTC" and series[0]["kalshi_mid"] == 0.7
    summary = json.loads(zf.read("summary.json"))
    assert summary["results"]["daily"][0]["settled"] == 1
    for name in names:
        assert API_KEY not in zf.read(name).decode()


def test_kalshi_feed_latency_uses_its_timestamp(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    w = window()
    setup_window(engine, w)
    frame = json.loads(kalshi_delta(w.kalshi_ticker, "no", 0.05, 1))
    frame["msg"]["ts_ms"] = at(OPEN, 10) // 1_000_000 - 80
    engine.handle_line(at(OPEN, 10), "k", json.dumps(frame))
    assert engine.metrics.snapshot()["feed_ms_k"]["p50"] == pytest.approx(80, abs=1)


def test_status_shows_delta_rule_preview_and_book_ages(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    w = window()
    setup_window(engine, w)  # gap +0.26, YES ask 0.46
    engine.handle_line(at(OPEN, 10), "p", poly_change(w.no_token, 0.55, 5, "BUY"))
    btc = engine.status()["assets"]["BTC"]
    assert btc["gap"] == pytest.approx(0.26)
    assert btc["preview"] == {
        "action": "none",
        "reason": "too_early",
        "side": None,
        "entry_px": None,
    }
    assert btc["age_ms"]["poly_no"] == 0 and btc["age_ms"]["kalshi"] == pytest.approx(9500, abs=1)
    assert btc["decision"] is None
    engine.handle_line(at(OPEN, 25), "p", poly_change(w.no_token, 0.55, 6, "BUY"))
    btc = engine.status()["assets"]["BTC"]
    assert btc["preview"]["action"] == "enter" and btc["preview"]["entry_px"] == 0.46
    assert btc["decision"]["action"] == "enter" and btc["decision"]["side"] == "yes"


def test_state_route_is_small(tmp_path: Path) -> None:
    with _status_app(tmp_path, []) as client:
        body = client.get("/api/state").json()
    assert set(body) == {
        "fetched_at", "service_up", "kalshi_closed", "params", "feed_age_s", "rates", "assets"
    }  # fmt: skip
    assert body["params"]["gap_threshold"] == 0.2
