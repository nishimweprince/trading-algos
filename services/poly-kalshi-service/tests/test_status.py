from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import NS, OPEN, make_settings
from fastapi.testclient import TestClient

from poly_kalshi_service.results import SETTLE_DELAY_DEFAULT_S, open_trade_etas, paper_results
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
    for row in rows:
        row["epoch"] = "1"  # as the trader stamps them (PK_RESULTS_EPOCH default)
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


def test_trade_quality_drawdown_and_funnel_by_hand() -> None:
    # P&L in time order: +60-f, -50-f, +55-f, -45-f, -50-f, +60-f (f = fee at that price).
    plan = [(0.40, True), (0.50, False), (0.45, True), (0.45, False), (0.50, False), (0.40, True)]
    records: list[dict[str, Any]] = []
    for i, (px, win) in enumerate(plan):
        records += _trade(OPEN + 900 * i, px, win)
        window_row = {
            "type": "window",
            "asset": "BTC",
            "open_unix": OPEN + 900 * i,
            "identified": i != 5,
            "evals": 100,
            "reasons": {"invalid_leg": 10},
        }
        records.append(window_row)  # fmt: skip
    records.append({"type": "decision", "asset": "ETH", "open_unix": OPEN, "t_ns": OPEN * NS,
                    "action": "skip", "blocked": None})  # fmt: skip
    out = paper_results(records)
    fee = {0.40: 1.68, 0.45: 1.7325, 0.50: 1.75}
    pnl = [(100 - 100 * p if w else -100 * p) - fee[p] for p, w in plan]
    q = out["quality"]
    assert (q["n"], q["wins"], q["losses"]) == (6, 3, 3)
    assert q["avg_win"] == pytest.approx(sum(p for p in pnl if p > 0) / 3, abs=0.01)
    assert q["profit_factor"] == pytest.approx(
        sum(p for p in pnl if p > 0) / -sum(p for p in pnl if p <= 0), abs=1e-3
    )
    assert q["expectancy"] == pytest.approx(sum(pnl) / 6, abs=0.01)
    assert q["longest_losing_streak"] == 2
    assert q["largest_loss"] == pytest.approx(min(pnl), abs=0.01)
    assert q["fees"] == pytest.approx(sum(fee[p] for p, _ in plan), abs=0.01)
    assert q["gross_pnl"] == pytest.approx(sum(pnl) + q["fees"], abs=0.01)
    # The peak is after trade 3; trades 4 and 5 lose, so max drawdown is that run.
    cum, peak, worst = 0.0, 0.0, 0.0
    for p in pnl:
        cum += p
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    assert out["drawdown"]["max"] == pytest.approx(worst, abs=0.01)
    assert out["drawdown"]["current"] == pytest.approx(peak - cum, abs=0.01)
    f = out["funnel"]
    assert (f["windows"], f["crossings"], f["skipped_above_cap"], f["orders"], f["filled"],
            f["settled"]) == (6, 7, 1, 6, 6, 6)  # fmt: skip
    dq = out["data_quality"]
    assert dq["unidentified_windows"] == 1
    assert dq["invalid_leg_share"] == pytest.approx(0.1)
    assert out["daily"][0]["windows"] == 6


def test_open_trade_etas_lists_unsettled_fills_with_close_plus_median_delay() -> None:
    poly = {
        "type": "resolution",
        "asset": "BTC",
        "open_unix": OPEN,
        "venue": "poly",
        "outcome": "yes",
        "delay_s": 200.0,
        "epoch": "1",
    }
    kalshi = {
        "type": "resolution",
        "asset": "BTC",
        "open_unix": OPEN + 900,
        "venue": "kalshi",
        "outcome": "no",
        "epoch": "1",
    }
    zero = {
        "type": "fill",
        "asset": "ETH",
        "open_unix": OPEN + 900,
        "t_ns": OPEN * NS,
        "side": "no",
        "shares": 0.0,
        "ordered": 50,
        "vwap": None,
        "epoch": "1",
    }
    records = _trade(OPEN, 0.40, True) + _trade(OPEN + 900, 0.50, False, settled=False)
    out = open_trade_etas([*records, poly, kalshi, zero])
    assert out["settle_delay_s"] == 200.0
    assert out["delay_samples"] == 1 and out["delay_default"] is False
    (t,) = out["trades"]  # the settled BTC trade and the zero-share ETH fill are out
    assert (t["asset"], t["side"], t["shares"]) == ("BTC", "yes", 100.0)
    assert t["close_unix"] == OPEN + 1800 and t["eta_unix"] == OPEN + 2000
    assert t["kalshi_outcome"] == "no"


def test_open_trade_etas_uses_default_delay_without_samples() -> None:
    out = open_trade_etas(_trade(OPEN, 0.40, True, settled=False))
    assert out["settle_delay_s"] == SETTLE_DELAY_DEFAULT_S
    assert out["delay_samples"] == 0 and out["delay_default"] is True
    assert out["trades"][0]["eta_unix"] == OPEN + 900 + SETTLE_DELAY_DEFAULT_S


def test_summary_includes_open_trade_etas(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    journal = settings.state_dir / "journal"
    journal.mkdir(parents=True)
    rows = _trade(OPEN, 0.4, True, settled=False) + [
        {
            "type": "resolution",
            "asset": "BTC",
            "open_unix": OPEN,
            "venue": "poly",
            "outcome": "yes",
            "delay_s": 100.0,
            "epoch": "1",
        }
    ]
    (journal / "2026-10-06.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    async def fetch() -> dict[str, Any]:
        return {"execution_mode": "paper"}

    app = create_status_app(settings, fetch=fetch, poll=False)
    with TestClient(app) as client:
        body = client.get("/api/summary").json()
    (t,) = body["open_trades"]["trades"]
    assert t["eta_unix"] == OPEN + 1000
    assert body["open_trades"]["delay_samples"] == 1


def test_ledger_snapshot_reports_exposure_and_settlement_wait() -> None:
    from poly_kalshi_service.ledger import Ledger, Position

    ledger = Ledger.open(None, 1000.0)
    ledger.buy(Position("BTC", OPEN, "yes", 100, 40.0, 1.68, 0))
    ledger.buy(Position("ETH", OPEN + 900, "no", 50, 25.0, 0.9, 0))
    snap = ledger.snapshot((OPEN + 900 + 900 + 30) * NS)
    assert snap["start_cash"] == 1000.0
    assert snap["largest_open_cost"] == pytest.approx(41.68)
    assert snap["oldest_open_age_s"] == 930  # BTC's window closed at OPEN + 900
