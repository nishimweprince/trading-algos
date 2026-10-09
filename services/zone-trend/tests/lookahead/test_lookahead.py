"""Look-ahead guards: nothing before time T may depend on data after T.

Each run records a per-bar digest of everything the core decides (trend
states, zones with roles and strengths, FSM states, signals, rejections), then:

- truncation: a run on data cut at T matches the full run up to T;
- HTF perturbation: rewriting every 4H and 1D bar that closes after T leaves
  everything up to T unchanged.

On synthetic regime-switching data in CI, and on real BTCUSDT when it has been
synced (``ZT_DATA_DIR`` or ``./data``).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from zone_trend.config import load_config
from zone_trend.core.replay import replay
from zone_trend.data.store import DataPaths, load

CFG = load_config()
H = 3_600_000


def synthetic(n_hours: int, seed: int) -> pd.DataFrame:
    """Trending and ranging regimes with volume bursts on big moves."""
    rng = np.random.default_rng(seed)
    regimes = rng.integers(0, 3, n_hours // 200 + 1).repeat(200)[:n_hours]
    drift = np.array([0.0006, -0.0006, 0.0])[regimes]
    ret = drift + rng.normal(0, 0.006, n_hours)
    close = 1000 * np.exp(np.cumsum(ret))
    open_ = np.concatenate([[1000.0], close[:-1]])
    wick = np.abs(rng.normal(0, 0.003, (2, n_hours)))
    high = np.maximum(open_, close) * (1 + wick[0])
    low = np.minimum(open_, close) * (1 - wick[1])
    volume = 100 * (1 + 50 * np.abs(ret)) * rng.uniform(0.7, 1.3, n_hours)
    t = pd.Timestamp("2021-01-01", tz="UTC").value // 1_000_000 + np.arange(n_hours) * H
    return pd.DataFrame(
        {
            "open_time": t,
            "close_time": t + H - 1,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


def resample(h1: pd.DataFrame, ms: int) -> pd.DataFrame:
    key = h1["open_time"] // ms * ms
    g = h1.groupby(key)
    out = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
            "n": g.size(),
        }
    )
    out = out[out["n"] == ms // H].drop(columns="n")
    out["open_time"] = out.index.astype("int64")
    out["close_time"] = out["open_time"] + ms - 1
    return out.reset_index(drop=True)


def digest(h1, h4, d1) -> list[tuple]:
    rows = []
    for core, step in replay("TEST", h1, h4, d1, CFG):
        zones = tuple((z.id, z.role, round(z.strength, 9), z.lo, z.hi) for z in core.zones.zones)
        fsms = tuple(sorted((k, f.state) for k, f in core.fsm.items()))
        rows.append(
            (
                step.bar.close_time,
                core.trend.value,
                core.structure[next(iter(core.structure))].trend.value,
                zones,
                fsms,
                tuple(s.id for s in step.result.signals),
                tuple((r.zone_id, r.reason) for r in step.result.rejected),
            )
        )
    return rows


def frames(h1: pd.DataFrame):
    return h1, resample(h1, 4 * H), resample(h1, 24 * H)


def assert_prefix_equal(full: list[tuple], cut: list[tuple], t_cut: int) -> None:
    prefix = [r for r in full if r[0] <= t_cut]
    assert len(prefix) == len(cut)
    for a, b in zip(prefix, cut, strict=True):
        assert a == b, f"diverged at {a[0]}"


def truncate(df: pd.DataFrame, t_cut: int) -> pd.DataFrame:
    return df[df["close_time"] <= t_cut].reset_index(drop=True)


def perturb_after(df: pd.DataFrame, t_cut: int) -> pd.DataFrame:
    out = df.copy()
    later = out["close_time"] > t_cut
    for col in ("open", "high", "low", "close"):
        out.loc[later, col] *= 1.1
    out.loc[later, "volume"] *= 3
    return out


N = 24 * 400  # 400 days: enough for zones, swings and the volatility history


@pytest.fixture(scope="module")
def synthetic_full():
    h1, h4, d1 = frames(synthetic(N, seed=7))
    return (h1, h4, d1), digest(h1, h4, d1)


def test_full_run_produces_decisions(synthetic_full):
    _, rows = synthetic_full
    assert any(r[3] for r in rows), "no zones at all: the fixture exercises nothing"
    assert any(r[5] or r[6] for r in rows), "no signals or rejections: widen the fixture"


@settings(
    max_examples=4, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(frac=st.floats(min_value=0.4, max_value=0.95))
def test_truncation_never_changes_the_past(synthetic_full, frac):
    (h1, h4, d1), full = synthetic_full
    t_cut = int(h1["close_time"].iloc[int(len(h1) * frac)])
    cut = digest(truncate(h1, t_cut), truncate(h4, t_cut), truncate(d1, t_cut))
    assert_prefix_equal(full, cut, t_cut)


def test_future_htf_bars_do_not_leak(synthetic_full):
    (h1, h4, d1), full = synthetic_full
    t_cut = int(h1["close_time"].iloc[int(len(h1) * 0.7)])
    changed = digest(h1, perturb_after(h4, t_cut), perturb_after(d1, t_cut))
    assert_prefix_equal(full, [r for r in changed if r[0] <= t_cut], t_cut)
    assert changed != full  # the perturbation is visible after the cut


@pytest.mark.data
def test_real_btc_truncated_at_2023_matches_full_run():
    paths = DataPaths.from_env()
    if not paths.months("klines", "BTCUSDT", "1h"):
        pytest.skip("BTCUSDT not synced")
    end = pd.Timestamp("2024-01-01", tz="UTC").value // 1_000_000
    h1, h4, d1 = (load(paths, "klines", "BTCUSDT", tf, end_ms=end) for tf in ("1h", "4h", "1d"))
    full = digest(h1, h4, d1)
    t_cut = pd.Timestamp("2023-01-01", tz="UTC").value // 1_000_000 - 1
    cut = digest(truncate(h1, t_cut), truncate(h4, t_cut), truncate(d1, t_cut))
    assert_prefix_equal(full, cut, t_cut)
    assert any(r[5] for r in full if r[0] <= t_cut)
