"""ofi-status: the read-only page for colleagues. Builders, app, and no leaks."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from ofi_scalper_service import status as builders
from ofi_scalper_service.daily_check import write_summary
from ofi_scalper_service.status_app import create_status_app
from ofi_scalper_service.trades import JsonlDaily
from tests.conftest import make_settings

TODAY = date(2026, 10, 20)


def _fake(kind: str, n: int) -> str:
    """A planted value for the leak test. Built at runtime, not written as a literal,
    so secret scanners do not mistake the test's fakes for real credentials."""
    return "-".join(("sentinel", kind, f"{n:04d}"))


API, KEY, SECRET, BOT, NOTIFY = (
    _fake("api", 1),
    _fake("venue", 2),
    _fake("venue-s", 3),
    _fake("bot", 4),
    _fake("notify", 5),
)
WALLET = "9876543.21"
SENTINELS = [API, KEY, SECRET, BOT, NOTIFY, WALLET, "binance_secret_alias", "/secret/path/raw"]


def daily(day: str, *, ok: bool = True, host: str = "azure-tokyo", mode: str = "diff") -> dict:
    return {
        "date": day,
        "files": 49,
        "hosts": [host],
        "book_mode": mode,
        "ok": ok,
        "problems": [] if ok else ["BTCUSDT: 2 missing hour(s) [3, 4]"],
        "symbols": {
            "BTCUSDT": {
                "missing_hours": [] if ok else [3, 4],
                "mb": 800.0,
                "lines": 30_000_000,
                "depth_breaks": 1,
                "unrecovered_breaks": 0,
                "agg_trade_gaps": 0,
                "max_silence_ms": 1200.0,
            },
            "ETHUSDT": {"missing_hours": [], "mb": 600.0, "lines": 20_000_000, "depth_breaks": 0},
        },
    }


def scalper_status() -> dict[str, Any]:
    """What /v1/status returns, with secrets planted where they could leak."""
    return {
        "ready": True,
        "execution_mode": "testnet",
        "streams": {
            "book_mode": "diff",
            "books": {"BTCUSDT": {"state": "live", "gaps": 0, "resyncs": 1, "last_update_id": 5}},
            "reconnects": {"public": 0, "market": 1},
        },
        "risk": {
            "halted": False,
            "halt": None,
            "pauses": {"BTCUSDT": []},
            "positions": {"BTCUSDT": 180.0},
            "equity": 9876543.21,
            "limits": {"daily_loss_limit_usd": 15},
        },
        "recorder": {"root": "/secret/path/raw", "open_files": ["/secret/path/raw/x.gz"]},
        "fees": {"account": {"feeTier": 0, "canTrade": True}},
        "key_check": {"status": "ok", "permissions": {"apiKey": KEY}},
        "heartbeat": {
            "uptime_s": 7200,
            "events_per_s": {"DepthUpdate": 410.5, "AggTrade": 95.0, "grid_samples": 20.0},
            "lag_ms": {"depth": [6.1, 21.4], "aggTrade": [7.0, 30.2]},
            "recorder": {"lines_per_s": 610, "disk_free_gb": 210.5, "disk_paused": False},
        },
        "model": {"version": "provisional-1-h5", "policy": {"threshold": 0.6}},
        "bridge": {
            "mode": "testnet",
            "controls": [{"response": {"api_key": API}}],
            "venue_quotes": {"url": "wss://demo"},
            "account": "binance_secret_alias",
            "wallet_balance_usdt": WALLET,
        },
        "alerts": {"recent": [BOT]},
    }


def settings_for(tmp_path: Path, **overrides: Any):
    return make_settings(
        tmp_path,
        API_KEY=API,
        BINANCE_FUTURES_API_KEY=KEY,
        BINANCE_FUTURES_API_SECRET=SECRET,
        OFI_TELEGRAM_BOT_TOKEN=BOT,
        OFI_TELEGRAM_ADMIN_USER_IDS="11",
        NOTIFICATION_API_KEY=NOTIFY,
        OFI_HOST_TAG="azure-tokyo",
        OFI_STATE_DIR=str(tmp_path / "state"),
        OFI_RESEARCH_DIR=str(tmp_path / "research"),
        OFI_MODEL_DIR=str(tmp_path / "models"),
        **overrides,
    )


# --- collection ------------------------------------------------------------------------


def test_collection_counts_eligible_days_and_milestones(tmp_path: Path) -> None:
    state = tmp_path / "state"
    for d in range(1, 8):
        write_summary(state, daily(f"2026-10-{d:02d}"))
    write_summary(state, daily("2026-10-08", ok=False))
    write_summary(state, daily("2026-10-09", host="mac"))
    write_summary(state, daily("2026-10-10", mode="partial"))
    # Today's recording so far: two closed hours with manifests, one open.
    raw = tmp_path / "raw" / "BTCUSDT" / "20261020"
    raw.mkdir(parents=True)
    for hour in ("00", "01", "02"):
        (raw / f"BTCUSDT_20261020_{hour}.gz").write_bytes(b"x" * 2_000_000)
    for hour in ("00", "01"):
        (raw / f"BTCUSDT_20261020_{hour}.gz.json").write_text(json.dumps({"lines": 100, "gaps": 0}))

    c = builders.collection(
        record_dir=tmp_path / "raw",
        state_dir=state,
        host_tag="azure-tokyo",
        min_free_disk_gb=10,
        today=TODAY,
    )
    assert c["recorded_days"] == 10 and c["eligible_days"] == 7
    reasons = {e["date"]: e["reason"] for e in c["excluded"]}
    assert "missing hour" in reasons["2026-10-08"]
    assert "recorded on mac" in reasons["2026-10-09"]
    assert "book mode partial" in reasons["2026-10-10"]
    today = c["days"][0]
    assert today["complete"] is False and today["symbols"]["BTCUSDT"]["hours"] == 3
    assert today["mb"] == 6.0
    assert c["days"][1]["date"] == "2026-10-10"  # newest first
    assert c["gb_per_day"] == 1.4
    assert c["disk_free_gb"] is not None and c["disk_runway_days"] is not None
    provisional = c["milestones"]["provisional"]
    assert provisional["eligible_days"] == 7 and provisional["target_days"] == 28
    assert provisional["earliest"] == "2026-11-10"  # 21 more clean days from 2026-10-20
    assert provisional["periods"] == 2  # ISO weeks 40 and 41
    assert c["milestones"]["binding"]["target_days"] == 120


def test_eligibility_tolerates_older_summaries() -> None:
    old = daily("2026-10-01")
    del old["hosts"], old["book_mode"]
    assert builders.eligibility(old, "azure-tokyo") == (True, "")


# --- health ----------------------------------------------------------------------------


def test_health_picks_only_listed_fields() -> None:
    h = builders.health(
        scalper_status(), {"ofi-scalper-service": "active"}, fetched_at=1.0, last_ok_at=1.0
    )
    assert (
        h["ready"] and h["execution_mode"] == "testnet" and h["model_version"] == "provisional-1-h5"
    )
    assert h["lag_ms"]["depth"] == {"p50": 6.1, "p99": 21.4}
    assert h["books"]["BTCUSDT"] == {"state": "live", "gaps": 0, "resyncs": 1}
    assert h["key_check"] == "ok" and h["recorder"]["lines_per_s"] == 610
    assert "equity" not in json.dumps(h) and "bridge" not in h


def test_health_when_the_scalper_is_down() -> None:
    h = builders.health(None, {"ofi-scalper-service": "failed"}, fetched_at=10.0, last_ok_at=None)
    assert h["scalper_reachable"] is False and h["last_ok_at"] is None
    assert h["services"]["ofi-scalper-service"] == "failed"


def test_history_ring() -> None:
    history = builders.History(points=3)
    for t in range(5):
        history.add(scalper_status() if t != 3 else None, float(t * 60))
    series = history.series()
    assert [p["t"] for p in series] == [120, 180, 240]
    assert series[1]["up"] is False and series[2]["depth_lag_p50"] == 6.1


# --- research and trading ----------------------------------------------------------------


def _research_files(tmp_path: Path) -> tuple[Path, Path]:
    research, models = tmp_path / "research", tmp_path / "models"
    (research / "splits").mkdir(parents=True)
    (research / "splits" / "p1.json").write_text(
        json.dumps(
            {
                "run": "p1",
                "period": "week",
                "walk_forward": ["2026-W41", "2026-W42", "2026-W43"],
                "holdout": ["2026-W44"],
                "days": {"2026-W41": ["2026-10-05", "2026-10-06"], "2026-W44": ["2026-10-26"]},
                "binding": False,
                "created_at": "2026-11-02T00:00:00+00:00",
            }
        )
    )
    cand = research / "candidates" / "p1" / "h5"
    cand.mkdir(parents=True)
    (cand / "candidate.json").write_text(
        json.dumps({"horizon_s": 5, "barrier_bp": 6, "features": ["a", "b"], "final_train_rows": 9})
    )
    (cand / "cv_report.json").write_text(
        json.dumps(
            {
                "folds": [{"validation": "2026-W42"}, {"validation": "2026-W43"}],
                "calibrated": {
                    "up": {
                        "brier": 0.11,
                        "reliability": [{"predicted": 0.2, "observed": 0.21, "n": 5}],
                    },
                    "none": {"brier": 0.2},
                    "down": {"brier": 0.12},
                },
            }
        )
    )
    (research / "candidates" / "p1" / "selection.json").write_text(
        json.dumps(
            {
                "rule": "max mean daily net",
                "chosen": {
                    "name": "h5-t0.55",
                    "trades": 40,
                    "net_usd": 1.5,
                    "candidate": "/secret/path/raw",
                },
                "grid": [{"name": "h5-t0.55", "trades": 40, "net_usd": 1.5}],
            }
        )
    )
    model = models / "p1-h5"
    model.mkdir(parents=True)
    (model / "gates.json").write_text(
        json.dumps(
            {
                "model_version": "p1-h5",
                "passed": False,
                "binding": False,
                "evaluated_at": "2026-11-02",
                "held_out_days": ["2026-10-26"],
                "gates": {"sharpe": {"value": 0.4, "threshold": 1.5, "passed": False}},
                "data": {"x.parquet": "deadbeef"},
            }
        )
    )
    (research / "REPORT.md").write_text("# p1-h5: does not clear the gates\n")
    (research / "holdout_ledger.json").write_text(
        json.dumps({"p1:2026-W44": {"candidate": "p1-h5", "at": "2026-11-02"}})
    )
    return research, models


def test_research_summary(tmp_path: Path) -> None:
    research, models = _research_files(tmp_path)
    r = builders.research(research, models)
    assert r["runs"][0]["held_out"] == ["2026-W44"] and r["runs"][0]["days"] == 3
    assert r["candidates"][0]["brier"] == {"down": 0.12, "none": 0.2, "up": 0.11}
    assert r["candidates"][0]["folds"] == 2
    assert r["selections"][0]["chosen"]["name"] == "h5-t0.55"
    assert "candidate" not in r["selections"][0]["chosen"]  # paths stay out
    assert r["models"][0]["gates"]["sharpe"] == {"value": 0.4, "threshold": 1.5, "passed": False}
    assert r["report"].startswith("# p1-h5") and r["holdout_evaluations"][0]["candidate"] == "p1-h5"


def test_research_empty(tmp_path: Path) -> None:
    r = builders.research(tmp_path / "none", tmp_path / "none")
    assert r == {
        "runs": [],
        "candidates": [],
        "selections": [],
        "models": [],
        "report": None,
        "holdout_evaluations": [],
    }


def test_trading_empty_and_with_records(tmp_path: Path) -> None:
    empty = builders.trading(tmp_path, model_loaded=False, today=TODAY)
    assert "No model loaded" in empty["note"] and empty["today"]["trades"] == 0
    t = int(1_792_454_400 * 1e9)  # 2026-10-20 UTC
    JsonlDaily(tmp_path / "trades").write(
        t,
        {
            "cycle_id": "BTCUSDT-1",
            "symbol": "BTCUSDT",
            "entry_qty_ordered": "0.003",
            "filled": True,
            "net_usd": 0.2,
            "net_bp": 11.0,
            "p": 0.7,
            "exit_reason": "take_profit",
            "legs": {"secret": "binance_secret_alias"},
        },
    )
    JsonlDaily(tmp_path / "signals").write(t, {"symbol": "BTCUSDT", "action": "enter", "gate": {}})
    full = builders.trading(tmp_path, model_loaded=True, today=TODAY)
    assert full["note"] is None and full["today"]["trades"] == 1
    assert full["recent_trades"][0]["exit_reason"] == "take_profit"
    assert "legs" not in full["recent_trades"][0]


def test_roadmap_file_is_valid() -> None:
    road = builders.roadmap()
    assert road and all(r["status"] in {"done", "active", "pending", "blocked"} for r in road)


# --- the app -----------------------------------------------------------------------------


async def client_for(settings: Any, status: dict[str, Any] | None) -> httpx.AsyncClient:
    async def fetch() -> dict[str, Any] | None:
        return status

    app = create_status_app(
        settings, fetch=fetch, units=lambda: {"ofi-scalper-service": "active"}, poll=False
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://status")


async def test_app_routes_headers_and_read_only(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    async with await client_for(settings, scalper_status()) as client:
        page = await client.get("/")
        assert page.status_code == 200 and "OFI scalper: status" in page.text
        assert "default-src 'none'" in page.headers["content-security-policy"]
        assert page.headers["cache-control"] == "no-store"
        body = (await client.get("/api/summary")).json()
        assert set(body) >= {"roadmap", "collection", "health", "research", "trading"}
        assert body["health"]["ready"] is True
        assert (await client.get("/api/history")).json() == {"points": []}
        assert (await client.get("/health/live")).json() == {"status": "ok"}
        for path in ("/v1/kill", "/v1/kill/ack", "/api/summary", "/"):
            assert (await client.post(path)).status_code in {404, 405}
        for path in ("/docs", "/openapi.json", "/v1/status", "/dashboard"):
            assert (await client.get(path)).status_code == 404


async def test_nothing_secret_reaches_a_viewer(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    _research_files(tmp_path)
    write_summary(tmp_path / "state", daily("2026-10-01"))
    async with await client_for(settings, scalper_status()) as client:
        text = (await client.get("/api/summary")).text + (await client.get("/")).text
    for sentinel in SENTINELS:
        assert sentinel not in text, sentinel


async def test_scalper_down_is_shown_not_fatal(tmp_path: Path) -> None:
    async with await client_for(settings_for(tmp_path), None) as client:
        body = (await client.get("/api/summary")).json()
    assert body["health"]["scalper_reachable"] is False
