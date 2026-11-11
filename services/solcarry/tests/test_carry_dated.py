from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from solcarry.analytics.carry_dated import basis_frame, build_cycles, dated_cost, run_dated


def _contract(sym, listing, expiry, prem0):
    idx = pd.date_range(listing, expiry - pd.Timedelta(hours=1), freq="h", tz="UTC")
    dte = (expiry - idx).total_seconds() / 86400
    T = dte[0]
    close = 100.0 * np.exp(prem0 * dte / T)  # converges linearly in log to the index
    return pd.DataFrame(
        {"ts": idx, "symbol": sym, "expiry_ts": expiry, "close": close, "dte_days": dte}
    )


@pytest.fixture
def market():
    e1 = pd.Timestamp("2025-03-28 08:00", tz="UTC")
    e2 = pd.Timestamp("2025-06-27 08:00", tz="UTC")
    e3 = pd.Timestamp("2025-09-26 08:00", tz="UTC")
    q = pd.concat(
        [
            _contract("SOLUSD_250328", e1 - pd.Timedelta(days=150), e1, 0.03),
            _contract("SOLUSD_250627", e2 - pd.Timedelta(days=150), e2, 0.02),
            _contract("SOLUSD_250926", e3 - pd.Timedelta(days=150), e3, 0.01),
        ],
        ignore_index=True,
    )
    idx = pd.date_range(q["ts"].min(), e3, freq="h", tz="UTC")
    index = pd.DataFrame({"close": 100.0}, index=idx)
    staking = pd.DataFrame({"y_lst": 0.06}, index=idx)
    hurdle = pd.DataFrame({"r_f": 0.04}, index=idx)
    return q, index, staking, hurdle, (e1, e2, e3)


def test_basis_cycle_and_chaining(market):
    q, index, *_, (e1, e2, e3) = market
    b = basis_frame(q, index)
    cyc = build_cycles(b, 100, pd.Timestamp("2026-01-01", tz="UTC"))
    assert list(cyc["contract"]) == ["SOLUSD_250328", "SOLUSD_250627", "SOLUSD_250926"]
    assert cyc["entry_ts"].iloc[0] <= e1 - pd.Timedelta(days=99)
    assert cyc["entry_ts"].iloc[1] == e1  # entered when previous expires
    assert cyc["entry_ts"].iloc[2] == e2
    r = cyc.iloc[1]
    assert r["basis_cycle"] == pytest.approx(np.log(r["F_entry"] / r["I_entry"]))
    assert r["a_cycle"] == pytest.approx(r["basis_cycle"] * 365 / r["dte_entry"])
    assert (cyc["status"] == "expired").all()


def test_variants(cfg, market):
    q, index, staking, hurdle, _ = market
    res = run_dated(
        cfg,
        q,
        index,
        staking,
        hurdle,
        q_hold=1.0,
        q_transfer=0.1,
        now=pd.Timestamp("2026-01-01", tz="UTC"),
    )
    c = res.cycles
    d1 = c[c.variant == "D1-BASIS"].iloc[1]
    assert d1["R"] == pytest.approx(d1["a_cycle"] - dated_cost(cfg, d1["years_cycle"], False))
    assert d1["X"] == pytest.approx(d1["R"] - 0.04)
    st = c[c.variant == "D2-STATIC"].iloc[1]
    phi_s = (1.0 + 0.05) / 2.0
    assert res.phi["D2-STATIC"] == pytest.approx(phi_s)
    assert st["R"] == pytest.approx(
        st["a_cycle"] + (1 - phi_s) * 0.06 - dated_cost(cfg, st["years_cycle"], True)
    )
    assert res.phi["D2-MANAGED"] == pytest.approx(max(0.10, 0.15 / 1.1))
    assert res.summaries["D2-MANAGED"]["flags"] == ["OPTIMISTIC"]
    assert res.summaries["D1-BASIS"]["cycles"] == 3


def test_open_cycle_unscored(cfg, market):
    q, index, staking, hurdle, (e1, e2, e3) = market
    res = run_dated(cfg, q, index, staking, hurdle, 1.0, 0.1, now=e3 - pd.Timedelta(days=10))
    last = res.cycles[(res.cycles.variant == "D1-BASIS")].iloc[-1]
    assert last["status"] == "open" and not last["scored"]
