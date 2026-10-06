from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import NS, OPEN, make_settings
from fastapi.testclient import TestClient

from poly_kalshi_service.results import paper_results
from poly_kalshi_service.status_app import create_status_app

DAY = 86_400


def _trade(open_unix: int, vwap: float, win: bool, *, settled: bool = True) -> list[dict[str, Any]]:
    t = (open_unix + 30) * NS
    shares, fee = 100.0, round(0.07 * vwap * (1 - vwap) * 100, 4)
    payout = shares if win else 0.0
    rows: list[dict[str, Any]] = [
        {"type": "decision", "asset": "BTC", "open_unix": open_unix, "t_ns": t,
         "action": "enter", "blocked": None, "gap": 0.25, "sec_into": 30.0},
        {"type": "fill", "asset": "BTC", "open_unix": open_unix, "t_ns": t + 1, "side": "yes",
         "shares": shares, "ordered": 108, "vwap": vwap, "cost": vwap * shares, "fee": fee,
         "touch_px": vwap},
    ]  # fmt: skip
    if settled:
        rows.append(
            {"type": "settle", "asset": "BTC", "open_unix": open_unix, "outcome": "yes" if win
             else "no", "payout": payout, "pnl": payout - vwap * shares - fee}
        )  # fmt: skip
    return rows


def test_paper_results_daily_curve_and_trades() -> None:
    records = (
        _trade(OPEN, 0.40, True)
        + _trade(OPEN + 900, 0.50, False)
        + _trade(OPEN + DAY, 0.45, True)
        + _trade(OPEN + DAY + 900, 0.45, True, settled=False)
        + [{"type": "decision", "asset": "ETH", "open_unix": OPEN, "t_ns": OPEN * NS,
            "action": "skip", "blocked": None}]
    )  # fmt: skip
    out = paper_results(records)
    newest, oldest = out["daily"]
    assert oldest["enter"] == 2 and oldest["skip"] == 1 and oldest["settled"] == 2
    assert oldest["win_rate"] == 0.5
    assert oldest["avg_vwap"] == pytest.approx(0.45)
    pnl1 = 60 - 0.07 * 0.4 * 0.6 * 100
    pnl2 = -50 - 0.07 * 0.25 * 100
    assert oldest["pnl"] == pytest.approx(pnl1 + pnl2, abs=0.01)
    assert newest["settled"] == 1 and newest["filled"] == 2
    assert newest["cumulative"] == pytest.approx(oldest["pnl"] + newest["pnl"], abs=0.01)
    assert len(out["curve"]) == 3  # the open trade is not on the curve
    assert out["trades"][0]["outcome"] is None  # newest first, still open
    assert out["trades"][-1]["pnl"] == pytest.approx(pnl1, abs=0.01)


def test_paper_results_thins_a_long_curve() -> None:
    records = [r for i in range(1000) for r in _trade(OPEN + 900 * i, 0.5, i % 2 == 0)]
    out = paper_results(records)
    assert len(out["curve"]) == 400
    assert out["curve"][-1][1] == pytest.approx(sum(r["pnl"] for r in records if "pnl" in r))


def test_viewers_do_not_add_load_on_the_trader(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    journal = settings.state_dir / "journal"
    journal.mkdir(parents=True)
    (journal / "2026-10-06.jsonl").write_text(
        "\n".join(json.dumps(r) for r in _trade(OPEN, 0.4, True)) + "\n"
    )
    calls = 0

    async def fetch() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"execution_mode": "paper", "secret": "x"}

    app = create_status_app(settings, fetch=fetch, poll=False)
    with TestClient(app) as client:
        for _ in range(20):
            body = client.get("/api/summary").json()
        response = client.get("/")
    assert calls == 1  # twenty page refreshes, one read of the trader
    assert body["service_up"] is True
    assert "secret" not in json.dumps(body)
    assert body["results"]["daily"][0]["settled"] == 1
    assert body["gates"]["fills"] == 1
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
