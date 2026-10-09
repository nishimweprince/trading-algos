from __future__ import annotations

import json

from fake_exchange import FakeExchange
from fastapi.testclient import TestClient
from test_oms import SIG, SIZE, Alerts

from zone_trend.live.oms import Oms
from zone_trend.live.ops import write_metrics
from zone_trend.live.status import create_app
from zone_trend.persistence.repo import Repo
from zone_trend.risk.sizing import Filters


async def _populate(db: str) -> Repo:
    ex = FakeExchange({"ETHUSDT": 2000.0})
    repo = Repo(db)
    oms = Oms(ex, repo, Alerts(), leverage=5, fee_taker=0.0005, clock_ms=lambda: ex.now)
    oms.filters = {"ETHUSDT": Filters(0.01, 0.001, 0.001, 20.0)}
    ex.sink = oms.on_event
    repo.record_signal(SIG, "accepted", "risk", "", "live", ex.now)
    await oms.open(SIG, SIZE)
    await oms.drain()
    repo.record_equity(ex.now, 500.0, 500.0, 500.0)
    repo.record_equity(ex.now + 3_600_000, 505.0, 505.0, 505.0)
    return repo


async def test_dashboard_health_metrics(tmp_path):
    db = f"sqlite:///{tmp_path / 'zt.db'}"
    repo = await _populate(db)
    runs = tmp_path / "runs"
    (runs / "abc123").mkdir(parents=True)
    (runs / "abc123" / "report.html").write_text("<!doctype html><p>report</p>")
    (runs / "abc123" / "meta.json").write_text(
        json.dumps(
            {
                "kind": "zone",
                "range": ["2021-01", "2026-09"],
                "symbols": ["ETHUSDT"],
                "slippage_mult": 1.0,
                "summary": {"trades": 3, "profit_factor": 1.4},
            }
        )
    )
    (runs / "abc123" / "config.yaml").write_text("timeframes:\n  entry: 1h\n")
    (runs / "wf-x").mkdir()
    (runs / "wf-x" / "acceptance.json").write_text(
        json.dumps(
            {
                "verdict": "fail",
                "criteria": [
                    {
                        "criterion": "Profit factor",
                        "value": "1.10",
                        "threshold": ">= 1.3",
                        "status": "fail",
                    }
                ],
            }
        )
    )
    client = TestClient(create_app(db, runs))

    assert client.get("/health").status_code == 503  # no heartbeat yet
    write_metrics(
        repo,
        __import__("time").time_ns() // 1_000_000,
        equity=505.0,
        mode="live",
        stage="micro",
        last_hour=1,
        api_weight_used=12,
    )
    assert client.get("/health").status_code == 200

    page = client.get("/").text
    assert "ETHUSDT" in page and "Open positions" in page and "accepted" in page
    research = client.get("/research").text
    assert "Profit factor" in research
    for label in ("Entry", "Slip", "Win %", "Net", "End eq", "1h", "1×", "data-c=", "data-v="):
        assert label in research, label
    assert "report" in client.get("/runs/abc123/report.html").text
    metrics = client.get("/metrics").text
    assert "zt_equity 505.0" in metrics and "zt_open_positions 1.0" in metrics
    assert client.get("/api/state").json()["positions"][0]["symbol"] == "ETHUSDT"
