"""Offline end-to-end: synthetic normalized tables -> validate -> analyze -> gate -> report."""

from __future__ import annotations

import json

import httpx
import numpy as np
import pandas as pd
import respx

from solcarry import pipeline
from solcarry.analytics import depth
from solcarry.gate.report import build_report
from solcarry.http import Http
from solcarry.lock import check_lock, write_lock
from solcarry.normalize.build import Tables
from solcarry.sources.jupiter import Jupiter

NOW = pd.Timestamp("2026-11-01", tz="UTC")


def _tables() -> Tables:
    rng = np.random.default_rng(0)
    idx = pd.date_range(
        pd.Timestamp("2025-01-01", tz="UTC"), NOW - pd.Timedelta(hours=1), freq="h", name="ts"
    )
    n = len(idx)
    close = 150 * np.exp(np.cumsum(rng.normal(0, 0.006, n)))
    spot = pd.DataFrame(
        {"open": close, "high": close * 1.004, "low": close * 0.996, "close": close}, index=idx
    )
    fb = pd.DataFrame({"f_h": rng.normal(0.08, 0.1, n) / 8760, "interval_h": 8}, index=idx)
    vidx = idx[idx >= pd.Timestamp("2026-07-10", tz="UTC")]
    fv = pd.DataFrame(
        {"f_h": 0.1095 / 8760, "raw_rate": 0.0013, "oracle_twap": 100.0, "n_records": 1},
        index=vidx,
    )
    vrec = pd.DataFrame(
        {
            "recordId": range(len(vidx)),
            "fundingRate": 0.0013,
            "fundingRateLong": 0.0013,
            "cumulativeFundingRateLong": np.cumsum(np.full(len(vidx), 0.0013)),
            "oraclePriceTwap": 100.0,
        }
    )
    index = pd.DataFrame({"close": close}, index=idx)
    frames = []
    for exp in pd.to_datetime(
        [
            "2025-03-28 08:00",
            "2025-06-27 08:00",
            "2025-09-26 08:00",
            "2025-12-26 08:00",
            "2026-03-27 08:00",
            "2026-06-26 08:00",
            "2026-09-25 08:00",
            "2026-12-25 08:00",
        ],
        utc=True,
    ):
        sel = (idx >= exp - pd.Timedelta(days=180)) & (idx < exp)
        dte = (exp - idx[sel]).total_seconds() / 86400
        frames.append(
            pd.DataFrame(
                {
                    "ts": idx[sel],
                    "symbol": f"SOLUSD_{exp:%y%m%d}",
                    "expiry_ts": exp,
                    "close": close[sel] * np.exp(0.08 * dte / 365),
                    "dte_days": dte,
                }
            )
        )
    quarterly = pd.concat(frames, ignore_index=True)
    sidx = idx[idx >= pd.Timestamp("2025-03-26", tz="UTC")]
    staking = pd.DataFrame({"y_lst": np.log(1.07), "y_native": np.log(1.07) / 0.96}, index=sidx)
    hurdle = pd.DataFrame({"r_f": np.log(1.04)}, index=idx)
    lending = pd.DataFrame({"r_res_aave": 0.04, "r_res_kamino": 0.06}, index=idx)
    expiry_1m = pd.DataFrame(
        columns=["ts", "symbol", "expiry_ts", "f_close", "f_volume", "i_close"]
    )
    fhl = pd.DataFrame(
        {"f_h": rng.normal(0.10, 0.08, n) / 8760, "premium": 0.0, "n_records": 1}, index=idx
    )
    return Tables(spot, fv, vrec, fb, index, quarterly, staking, hurdle, lending, expiry_1m, fhl)


@respx.mock
def test_end_to_end(cfg, paths):
    _tables().save(paths.normalized)
    respx.get(url__startswith="https://lite-api.jup.ag/").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "inAmount": req.url.params["amount"],
                "outAmount": str(int(int(req.url.params["amount"]) * 1.3)),
                "priceImpactPct": "0",
                "routePlan": [{"swapInfo": {"label": "Manifest"}, "percent": 100}],
            },
        )
    )
    cfg.lending.reserve_yield_source = "aave"
    cfg.depth.min_interval_s = 0
    run = pipeline.new_run(cfg, paths)
    snap = pipeline.depth_snapshot(cfg, paths, run)
    assert (snap["impact"].abs() < 1e-9).all()

    val = pipeline.validate(cfg, paths, run)
    assert val.ok, val.to_json()
    assert val.flag("velocity_floor")  # pinned at floor in the synthetic data

    out = pipeline.analyze(cfg, paths, run)
    assert set(out["summaries"]) == set(cfg.variants)
    assert out["summaries"]["D1-BASIS"]["cycles"] >= 6

    write_lock(cfg, paths.lock_file)
    g = pipeline.gate(cfg, paths, run, check_lock(cfg, paths.lock_file))
    status = {s["variant"]: s["status"] for s in g["scores"]}
    assert status["P-LST-VEL"] == "INSUFFICIENT_DATA"
    assert status["P-LST-HL"] in ("PASS", "FAIL") and status["P-LST-HL-G"] in ("PASS", "FAIL")
    assert out["summaries"]["P-LST-HL-G"]["gating"]["on_share"] > 0.5
    assert status["D1-BASIS"] in ("PASS", "FAIL")
    assert g["result"] in ("CONTINUE", "KILL") and not g["unlocked"]

    rp = build_report(cfg, paths, run)
    text = rp.read_text()
    assert "EVIDENCE TIER: reconstructed" in text and "## 8. Caveats" in text
    for png in ("chart_funding.png", "chart_basis.png", "chart_runups.png", "chart_excess.png"):
        assert (run / png).exists()
    ui = json.loads((run / "ui.json").read_text())
    assert ui["gate"]["result"] == g["result"] and ui["xw30"] and ui["basis"] and ui["cycles"]
    assert ui["gating"] and "hyperliquid_7d" in ui["funding_daily"]
    assert "Funding gate (*-G)" in text
    assert pipeline.resolve_run(paths, None) == run


def test_depth_impact_and_history(cfg, paths):
    class FakeJup(Jupiter):
        def quote(self, input_mint, amount_tokens):  # type: ignore[override]
            from solcarry.sources.jupiter import Quote

            px = 1.3 * (1 - 0.000002 * amount_tokens)
            return Quote(int(amount_tokens * 1e9), int(amount_tokens * px * 1e9), 0.0, "X:100%")

    snap = depth.snapshot(cfg, FakeJup(cfg, Http(cfg.http)))
    hist = depth.append_history(snap, paths.results)
    hist = depth.append_history(snap, paths.results)
    assert len(hist) == 2 * len(snap)
    imp, ts = depth.latest_impact(hist, "jitoSOL", 1000)
    assert imp < 0 and ts is not None
    assert np.isnan(depth.latest_impact(hist, "nope", 1000)[0])
