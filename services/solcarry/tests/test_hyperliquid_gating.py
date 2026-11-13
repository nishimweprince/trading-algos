"""Hyperliquid source + normalization, and the funding gate."""

from __future__ import annotations

import json
from datetime import date

import httpx
import numpy as np
import pandas as pd
import pytest
import respx

from solcarry.analytics.carry_perp import (
    gate_signal,
    gate_states,
    gated_carry,
    hourly_carry,
    run_perp,
    switch_cost,
)
from solcarry.http import Http
from solcarry.lock import preregistration_hash
from solcarry.normalize.funding import hyperliquid_hourly
from solcarry.sources.hyperliquid import Hyperliquid, parse_funding

HL = "https://api.hyperliquid.xyz/info"
H = 3_600_000


def _rec(t_ms: int, rate: float = 1.25e-5) -> dict:
    return {"coin": "SOL", "fundingRate": str(rate), "premium": "0.0", "time": t_ms + 37}


def test_parse_funding():
    df = parse_funding([_rec(2 * H), _rec(H, -1e-5)])
    assert list(df["time"]) == [H + 37, 2 * H + 37]
    assert df["fundingRate"].iloc[0] == pytest.approx(-1e-5)
    assert parse_funding([]).empty


@respx.mock
def test_sync_pages_months_and_caches(cfg, paths):
    cfg.hyperliquid.page_limit = 3
    cfg.hyperliquid.min_interval_s = 0
    t0 = int(pd.Timestamp("2026-09-30 20:00", tz="UTC").value // 1_000_000)
    allrec = [_rec(t0 + i * H) for i in range(12)]  # Sep 30 20:00 -> Oct 1 07:00
    bodies = []

    def handler(request):
        b = json.loads(request.content)
        bodies.append(b)
        st, en = b["startTime"], b.get("endTime", 10**15)
        sel = [r for r in allrec if st <= r["time"] <= en]
        return httpx.Response(200, json=sel[: cfg.hyperliquid.page_limit])

    respx.post(HL).mock(side_effect=handler)
    hl = Hyperliquid(cfg, Http(cfg.http), paths.raw)
    n = hl.sync(today=date(2026, 10, 1))
    assert n == 2  # Sep (past) + Oct (current)
    df = hl.load()
    assert len(df) == 12 and df["time"].is_monotonic_increasing
    before = len(bodies)
    assert hl.sync(today=date(2026, 10, 1)) == 1  # Sep cached, only current month refetched
    assert all(b["startTime"] >= t0 - 31 * 24 * H for b in bodies[before:])


def test_hourly_drops_8h_prelude_and_keeps_sign():
    t = [i * 8 * H for i in range(5)]  # 8-hourly prelude
    start = t[-1] + 8 * H
    t += [start + i * H for i in range(48)]
    raw = parse_funding([_rec(x, 1e-5) for x in t])
    out = hyperliquid_hourly(raw)
    assert len(out) == 48
    assert out.index.min() == pd.Timestamp(start, unit="ms", tz="UTC")
    assert (out["f_h"] > 0).all()


def test_gate_signal_has_no_lookahead():
    idx = pd.date_range("2025-01-01", periods=24 * 10, freq="h", tz="UTC")
    f = pd.Series(0.0, index=idx)
    f.iloc[24 * 5] = 1.0  # spike
    sig = gate_signal(f, 1, 0.9)
    assert sig.iloc[24 * 5] == 0.0  # the spike hour itself is not visible
    assert sig.iloc[24 * 5 + 1] > 0


def test_gate_states_hysteresis():
    e = pd.Series([np.nan, 0.01, 0.03, 0.0, -0.01, -0.03, 0.01, 0.05])
    assert gate_states(e, 0.02).tolist() == [False, False, True, True, True, False, False, True]


def test_gated_carry_off_is_hurdle_and_switch_cost():
    idx = pd.date_range("2025-01-01", periods=48, freq="h", tz="UTC")
    c = lambda v: pd.Series(v, index=idx)  # noqa: E731
    h = hourly_carry(c(0.2 / 8760), c(0.05), c(0.04), 0.0, 0.1, 1.2, 0.001)
    sig = c(-1.0)
    sig.iloc[24:] = 1.0
    g = gated_carry(h, sig, 0.02, 0.001, 0.002, 0.1, 1.2)
    assert (g["x_h"].iloc[:24] == 0).all()
    assert g["switch"].sum() == 1 and bool(g["switch"].iloc[24])
    on_rate = (0.05 + 0.2 - 0.001) / 1.2
    assert g["R_h"].iloc[25] == pytest.approx(on_rate)
    assert g["R_h"].iloc[24] == pytest.approx(on_rate - 0.002 / 1.2 * 8760)


def test_run_perp_gated_vs_ungated(cfg):
    """Funding alternates +30%/-30% by month: the gate should beat always-on."""
    idx = pd.date_range("2025-04-01", periods=24 * 360, freq="h", tz="UTC")
    month = (np.arange(len(idx)) // (24 * 30)) % 2
    f = pd.Series(np.where(month == 0, 0.30, -0.30) / 8760, index=idx)
    funding = pd.DataFrame({"f_h": f})
    staking = pd.DataFrame({"y_lst": 0.05, "y_native": 0.052}, index=idx)
    hurdle = pd.DataFrame({"r_f": 0.04}, index=idx)
    close = pd.Series(100.0, index=idx)
    a = run_perp(cfg, "P-LST-HL", funding, staking, hurdle, pd.DataFrame(), close, 0.05)
    g = run_perp(cfg, "P-LST-HL-G", funding, staking, hurdle, pd.DataFrame(), close, 0.05)
    assert a.summary["flags"] == [] and "GATED" in g.summary["flags"]
    assert 0.3 < g.summary["gating"]["on_share"] < 0.7
    assert g.summary["gating"]["switches"] >= 10
    assert g.summary["excess"]["p5"] > a.summary["excess"]["p5"]
    assert g.cost == pytest.approx(cfg.costs.resize_bps_per_year / 1e4)
    assert switch_cost(cfg) == pytest.approx((10 + 10) / 1e4)


def test_gating_is_preregistered(cfg):
    h0 = preregistration_hash(cfg)
    cfg.gating.hysteresis_annual = 0.03
    assert preregistration_hash(cfg) != h0
