"""Stage 2 building blocks: labels, PCA, splits, calibration, scores, latency, gates."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

pd = pytest.importorskip("pandas")

from ofi_scalper_service.model import Calibrator  # noqa: E402
from ofi_scalper_service.risk import RiskLimits  # noqa: E402
from research.backtest import Latency, aggregate, risk_stats  # noqa: E402
from research.cv import (  # noqa: E402
    HoldoutUsed,
    Split,
    load_or_create_split,
    period_of,
    purge,
    record_holdout_use,
)
from research.dataset import add_labels, apply_pca, fit_pca, triple_barrier  # noqa: E402
from research.gates import gate_table  # noqa: E402
from research.scores import PrecomputedScorer, calibrate  # noqa: E402
from research.train import brier_and_reliability, isotonic_knots  # noqa: E402

G = 100_000_000  # one grid step
NAN = float("nan")


def labels(mids, *, horizon_s=0.3, barrier_bp=10, times=None):
    t = np.array(times if times is not None else [i * G for i in range(len(mids))])
    return triple_barrier(t, np.array(mids, float), horizon_s=horizon_s, barrier_bp=barrier_bp)


# --- labels ---------------------------------------------------------------------------


def test_the_first_barrier_touched_wins() -> None:
    # 10 bp of 100 is 0.1; horizon 3 steps. Up is touched before down here.
    assert labels([100.0, 100.1, 99.8, 100.0, 100.0])[0] == 2
    assert labels([100.0, 99.9, 100.2, 100.0, 100.0])[0] == 0


def test_up_down_none_exactly() -> None:
    assert labels([100, 100.1, 100, 100, 100])[0] == 2
    assert labels([100, 99.9, 100, 100, 100])[0] == 0
    assert labels([100, 100.05, 99.95, 100, 100])[0] == 1


def test_a_gap_or_reset_before_a_touch_has_no_label() -> None:
    assert np.isnan(labels([100, 100, NAN, 100.2, 100, 100])[0])
    gap = [0, G, 4 * G, 5 * G, 6 * G, 7 * G]  # 3 steps missing after the second sample
    assert np.isnan(labels([100, 100, 100.2, 100, 100, 100], times=gap)[0])
    # A touch before the break still counts.
    assert labels([100, 100.1, NAN, 100, 100, 100])[0] == 2


def test_the_end_of_data_has_no_label() -> None:
    got = labels([100, 100, 100, 100, 100])
    assert got[1] == 1 and np.isnan(got[2]) and np.isnan(got[4])


def test_labels_per_symbol() -> None:
    df = pd.DataFrame(
        {
            "symbol": ["A"] * 4 + ["B"] * 4,
            "t_ns": [i * G for i in range(4)] * 2,
            "mid": [100, 100.2, 100, 100, 50, 49.9, 50, 50],
        }
    )
    out = add_labels(df, (0.2,), barrier_bp=10)
    assert list(out["label_0.2s"][[0, 4]]) == [2, 0]


# --- PCA --------------------------------------------------------------------------------


def _levels(n: int, rng: np.random.Generator) -> pd.DataFrame:
    common = rng.normal(size=n)
    data = {}
    for window in (100, 1000, 5000, 30000):
        for level in range(1, 11):
            data[f"ofi_l{level}_{window}ms"] = common * (11 - level) + rng.normal(size=n) * 0.1
    return pd.DataFrame(data)


def test_pca_weights_follow_the_common_factor_and_recompute_integrated_ofi() -> None:
    df = _levels(500, np.random.default_rng(1))
    weights = fit_pca(df)
    assert len(weights) == 10 and sum(abs(w) for w in weights) == pytest.approx(1.0)
    assert all(w > 0 for w in weights)
    out = apply_pca(df, weights)
    row = df.iloc[0]
    expected = sum(w * row[f"ofi_l{i}_1000ms"] for i, w in enumerate(weights, start=1))
    assert out["ofi_int_1000ms"].iloc[0] == pytest.approx(expected)


# --- splits and the held-out period ------------------------------------------------------


def test_periods() -> None:
    assert period_of("2026-10-05", "week") == "2026-W41"
    assert period_of("2026-10-05", "month") == "2026-10"
    with pytest.raises(ValueError):
        period_of("2026-10-05", "day")


def test_split_is_fixed_once_made_and_needs_enough_periods(tmp_path: Path) -> None:
    days = [f"2026-10-{d:02d}" for d in range(5, 33 - 3)]  # 2026-10-05 .. 2026-10-29: 4 weeks
    split = load_or_create_split(tmp_path, "p1", days=days, period="week", binding=False)
    assert split.walk_forward == ("2026-W41", "2026-W42", "2026-W43") and split.holdout == (
        "2026-W44",
    )
    assert split.folds == [(("2026-W41",), "2026-W42"), (("2026-W41", "2026-W42"), "2026-W43")]
    more = days + ["2026-11-02", "2026-11-03"]
    again = load_or_create_split(tmp_path, "p1", days=more, period="week", binding=False)
    assert again.holdout == ("2026-W44",)  # new data does not move the held-out period
    with pytest.raises(ValueError, match="3 walk-forward"):
        load_or_create_split(tmp_path, "p2", days=days[:10], period="week", binding=False)


def test_purge_drops_rows_whose_label_reaches_validation() -> None:
    start = 10 * 60 * 10**9
    train = pd.DataFrame({"t_ns": [0, start - 6 * 60 * 10**9, start - 5 * 60 * 10**9]})
    kept = purge(train, start, horizon_s=30)
    # 4 min + 30 s + 5 min embargo ends before the start; 5 min + 30 s + 5 min does not.
    assert list(kept["t_ns"]) == [0, start - 6 * 60 * 10**9]


def test_the_held_out_period_is_evaluated_once(tmp_path: Path) -> None:
    split = Split("p1", "week", ("a", "b", "c"), ("d",), {}, False, "now")
    ledger = tmp_path / "ledger.json"
    record_holdout_use(ledger, split, "v1")
    with pytest.raises(HoldoutUsed, match="already evaluated"):
        record_holdout_use(ledger, split, "v2")
    assert json.loads(ledger.read_text())["p1:d"]["candidate"] == "v1"


# --- calibration and scores ---------------------------------------------------------------


def test_isotonic_pool_adjacent_violators() -> None:
    knots = isotonic_knots(np.array([0.1, 0.2, 0.3, 0.4]), np.array([0.0, 1.0, 0.0, 1.0]))
    assert knots == {"x": [0.1, 0.25, 0.4], "y": [0.0, 0.5, 1.0]}
    flat = isotonic_knots(np.array([0.5, 0.5]), np.array([1.0, 1.0]))
    assert flat["x"] == [0.0, 1.0] and flat["y"] == [1.0, 1.0]


def test_vectorised_calibration_equals_the_live_calibrator() -> None:
    rng = np.random.default_rng(3)
    knots = {
        name: isotonic_knots(rng.random(300), (rng.random(300) < 0.4).astype(float))
        for name in ("down", "none", "up")
    }
    calibrator = Calibrator(knots)
    raw = rng.dirichlet([1, 1, 1], size=50)
    batch = calibrate(raw, calibrator)
    for row, out in zip(raw, batch, strict=True):
        live = calibrator({"down": row[0], "none": row[1], "up": row[2]})
        assert (live.down, live.none, live.up) == pytest.approx(tuple(out))


def test_precomputed_scorer_looks_up_and_misses() -> None:
    table = pd.DataFrame(
        {
            "symbol": ["BTCUSDT", "BTCUSDT"],
            "t_ns": [200, 100],
            "down": [0.1, 0.2],
            "none": [0.3, 0.3],
            "up": [0.6, 0.5],
        }
    )
    scorer = PrecomputedScorer(table)
    scores = scorer.score({"symbol": "BTCUSDT", "t_ns": 200})
    assert scores is not None and scores.up == 0.6 and scores.down == 0.1
    assert scorer.score({"symbol": "BTCUSDT", "t_ns": 150}) is None
    assert scorer.score({"symbol": "ETHUSDT", "t_ns": 100}) is None
    assert (scorer.hits, scorer.misses) == (1, 2)


def test_brier_and_reliability() -> None:
    probs = np.array([[0.0, 0.0, 1.0], [1.0, 0.0, 0.0]])
    report = brier_and_reliability(probs, np.array([2, 0]))
    assert report["up"]["brier"] == 0.0 and report["none"]["brier"] == 0.0


# --- latency, statistics, gates ------------------------------------------------------------


def test_latency_multiples_and_file(tmp_path: Path) -> None:
    latency = Latency(feed_ms=6.0, order_ms=10.0)
    assert latency.delay_ns(1) == 10_000_000
    assert latency.delay_ns(2) == 26_000_000  # one more feed latency, twice the order
    path = tmp_path / "latency.json"
    path.write_text(
        json.dumps(
            {"feed_latency_ms": {"depth": {"p50": 6.0}}, "rest_rtt_ms": {"signed": {"p50": 40.0}}}
        )
    )
    assert Latency.from_file(path) == Latency(6.0, 20.0)
    assert Latency.from_file(path, order_rtt_ms=30.0).order_ms == 15.0
    assert Latency.from_file(tmp_path / "missing.json") == Latency(0.0, 0.0)


def test_risk_stats() -> None:
    stats = risk_stats([10.0, -20.0, 5.0], capital_usd=100.0)
    assert stats["net_usd"] == -5.0 and stats["worst_day_usd"] == -20.0
    assert stats["max_drawdown_pct"] == pytest.approx(20 / 110 * 100, abs=1e-3)
    assert risk_stats([1.0], 100.0)["sharpe"] is None


def _trade(symbol: str, net: float, p: float = 0.7) -> dict:
    return {
        "symbol": symbol,
        "filled": True,
        "entry_qty_ordered": "0.003",
        "net_usd": net,
        "net_bp": net / 180 * 10_000,
        "fees_usd": 0.07,
        "p": p,
        "exit_reason": "take_profit" if net > 0 else "stop",
        "adverse_bp": {},
        "rtt_ms": {},
    }


def _results(per_day: dict[str, list[dict]], name: str = "base") -> list[dict]:
    return [
        {"day": day, "variants": {name: {"trades": trades, "halts": []}}}
        for day, trades in per_day.items()
    ]


def test_gate_table_pass_and_fail() -> None:
    days = [f"2026-10-{d:02d}" for d in range(26, 32)]
    winning = {day: [_trade("BTCUSDT", 0.2), _trade("ETHUSDT", 0.15)] * 3 for day in days}
    winning[days[0]] = winning[days[0]] + [_trade("BTCUSDT", -0.1)]
    agg = {}
    for name in ("base", "pessimistic", "latency_2x", "stress"):
        agg.update(aggregate(_results(winning, name), capital_usd=500.0))
    split = Split("p1", "week", ("a", "b", "c"), ("d",), {}, False, "now")
    selection = {"chosen": {"by_period_net_usd": {"b": 1.0, "c": 0.5}}}
    gates = gate_table(agg, split=split, selection=selection, daily_loss_limit_usd=15.0)
    assert all(g["passed"] for g in gates.values()), {
        k: g for k, g in gates.items() if not g["passed"]
    }

    agg["pessimistic"] = aggregate(
        _results({days[0]: [_trade("BTCUSDT", -1.0)]}, "pessimistic"), capital_usd=500.0
    )["pessimistic"]
    agg["base"]["by_symbol_net_usd"].pop("ETHUSDT")
    failed = gate_table(agg, split=split, selection=selection, daily_loss_limit_usd=15.0)
    assert not failed["pessimistic_queue_profitable"]["passed"]
    assert not failed["eth_positive"]["passed"]
    assert failed["btc_positive"]["passed"]


def test_limits_build_from_settings_for_jobs(tmp_path: Path) -> None:
    from research.backtest import job_settings
    from tests.conftest import make_settings

    common = job_settings(
        make_settings(tmp_path, OFI_BNB_FEE_DISCOUNT=True, OFI_MAKER_FEE_BP=2, OFI_TAKER_FEE_BP=5)
    )
    assert isinstance(common["limits"], RiskLimits)
    assert common["maker_bp"] == pytest.approx(1.8) and common["taker_bp"] == pytest.approx(4.5)
