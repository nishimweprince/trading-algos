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
    assert period_of("2026-10-05", "day") == "2026-10-05"
    with pytest.raises(ValueError):
        period_of("2026-10-05", "year")


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
    # Without a cost (or markouts) the signal-edge gate is informational only.
    assert gates.pop("signal_edge_covers_cost")["required"] is False
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


def test_shallow_gates_relax_what_one_held_out_day_cannot_show(tmp_path: Path) -> None:
    from research.gates import passed_all, tier_of

    days = ["2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06"]
    split = load_or_create_split(tmp_path, "s1", days=days, period="day", binding=False)
    assert split.walk_forward == tuple(days[:3]) and split.holdout == (days[3],)
    assert tier_of(split) == "shallow"

    held_out = {days[3]: [_trade("BTCUSDT", 0.2)] * 15 + [_trade("ETHUSDT", -0.05)] * 6}
    agg = {}
    for name in ("base", "pessimistic", "latency_2x", "stress"):
        agg.update(aggregate(_results(held_out, name), capital_usd=500.0))
    selection = {"chosen": {"by_period_net_usd": {days[1]: 1.0, days[2]: 0.5}}}
    gates = gate_table(agg, split=split, selection=selection, daily_loss_limit_usd=15.0)
    assert gates["sharpe"]["value"] is None and gates["sharpe"].get("required") is False
    assert not gates["eth_positive"]["passed"] and gates["eth_positive"]["required"] is False
    assert gates["min_trades"]["threshold"] == 20
    assert passed_all(gates), {k: g for k, g in gates.items() if not g["passed"]}

    # The same day under the weekly tier fails: no Sharpe, ETH negative.
    weekly = Split("p1", "week", ("a", "b", "c"), ("d",), {}, False, "now")
    assert not passed_all(
        gate_table(agg, split=weekly, selection=selection, daily_loss_limit_usd=15.0)
    )


def test_strided_keeps_every_nth_row_per_symbol() -> None:
    from research.train import _strided

    df = pd.DataFrame({"symbol": ["A"] * 5 + ["B"] * 4, "t_ns": list(range(5)) + list(range(4))})
    kept = _strided(df, 2)
    assert list(zip(kept["symbol"], kept["t_ns"], strict=True)) == [
        ("A", 0),
        ("A", 2),
        ("A", 4),
        ("B", 0),
        ("B", 2),
    ]


def test_half_latency_takes_the_feed_saving_off_the_order_delay() -> None:
    lat = Latency(feed_ms=4.0, order_ms=6.0)
    assert lat.delay_ns(1.0) == 6_000_000
    assert lat.delay_ns(2.0) == 16_000_000
    assert lat.delay_ns(0.5) == 1_000_000  # 0.5 x 6 - 0.5 x 4
    assert Latency(feed_ms=10.0, order_ms=2.0).delay_ns(0.5) == 0  # floored


def test_latency_sensitivity_reports_bp_gained_at_half_latency() -> None:
    from research.gates import latency_sensitivity

    day = {"2026-10-06": [_trade("BTCUSDT", 0.2)] * 5}
    faster = {"2026-10-06": [_trade("BTCUSDT", 0.3)] * 6}
    agg = {}
    agg.update(aggregate(_results(day, "base"), capital_usd=500.0))
    agg.update(aggregate(_results(day, "latency_2x"), capital_usd=500.0))
    agg.update(aggregate(_results(faster, "latency_0_5x"), capital_usd=500.0))
    out = latency_sensitivity(agg)
    assert out["0.5x"]["trades"] == 6 and out["1x"]["trades"] == 5
    assert out["bp_per_trade_gained_at_0.5x"] == round(0.1 / 180 * 10_000, 3)


def test_entry_timeout_sensitivity_compares_5s_with_3s() -> None:
    from research.backtest import ENTRY_TIMEOUT_MS, SHORTER_ENTRY_TIMEOUT_MS
    from research.gates import entry_timeout_sensitivity

    assert (ENTRY_TIMEOUT_MS, SHORTER_ENTRY_TIMEOUT_MS) == (5000, 3000)
    long_wait = {"2026-10-06": [_trade("BTCUSDT", 0.2)] * 6}
    short_wait = {"2026-10-06": [_trade("BTCUSDT", 0.2)] * 5}
    agg = {}
    agg.update(aggregate(_results(long_wait, "base"), capital_usd=500.0))
    agg.update(aggregate(_results(short_wait, "timeout_short"), capital_usd=500.0))
    out = entry_timeout_sensitivity(agg)
    assert out["5000ms"]["trades"] == 6 and out["3000ms"]["trades"] == 5
    assert "time_to_fill_ms" in out["3000ms"]
    assert out["shorter_no_worse"] is False  # one trade fewer, same edge each


def test_score_diagnostics_show_a_barrier_too_small_for_the_fees(tmp_path: Path) -> None:
    import pandas as pd

    from research.cv import Split
    from research.gates import _no_candidate_md
    from research.selection import score_diagnostics

    class FakeCandidate:
        horizon_s, barrier_bp, version = 30.0, 6.0, "h30"

        def oof_dir(self) -> Path:
            return tmp_path

    pd.DataFrame(
        {
            "symbol": ["BTCUSDT"] * 3,
            "t_ns": [1, 2, 3],
            "up": [0.50, 0.10, 0.05],
            "down": [0.10, 0.30, 0.05],
            "none": [0.40, 0.60, 0.90],
        }
    ).to_parquet(tmp_path / "2026-10-04.parquet")
    rows = score_diagnostics([FakeCandidate()], ["2026-10-04"], (0.45,), maker_bp=2.0)
    (row,) = rows
    assert row["above_threshold"] == 1 and row["entries_possible"] == 0
    assert row["gap_needed"] == round(4 / 6, 4) and row["gap_max"] == 0.4
    assert row["p_max"] == 0.5

    split = Split("shallow-1", "day", ("2026-10-03", "2026-10-04"), ("2026-10-05",), {}, False, "")
    report = _no_candidate_md(split, {"days": ["2026-10-04"], "diagnostics": rows, "grid": []})
    assert "No score at any horizon can pay the round trip" in report
    assert "| 30 s | 6.0 |" in report


def test_barriers_by_horizon_takes_one_value_or_one_per_horizon() -> None:
    from research.pipeline import barriers_by_horizon, candidate_dir_name

    assert barriers_by_horizon("6", [1.0, 30.0]) == {1.0: [6.0], 30.0: [6.0]}
    assert barriers_by_horizon("60:12,120:15,300:20", [60.0, 300.0]) == {
        60.0: [12.0],
        300.0: [20.0],
    }
    assert barriers_by_horizon("300:20|25,900:30|40", [300.0, 900.0]) == {
        300.0: [20.0, 25.0],
        900.0: [30.0, 40.0],
    }
    with pytest.raises(SystemExit, match="300"):
        barriers_by_horizon("60:12", [60.0, 300.0])
    # One barrier keeps the old directory name, so earlier runs' candidates are reused.
    assert candidate_dir_name(300.0, 20.0, several=False) == "h300"
    assert candidate_dir_name(300.0, 25.0, several=True) == "h300-b25"


def test_backtest_arms_parse_into_policy_fields() -> None:
    from research.backtest import parse_arm

    assert parse_arm("guard3:guard_z=3,guard_tfi=0.3,exit_mode=maker_chase") == (
        "guard3",
        (("guard_z", 3), ("guard_tfi", 0.3), ("exit_mode", "maker_chase")),
    )
    with pytest.raises(ValueError, match="not a policy field"):
        parse_arm("x:guard=3")
    with pytest.raises(ValueError, match="NAME:field"):
        parse_arm("guard3")


def test_feature_files_written_before_a_feature_existed_are_replayed(tmp_path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    from ofi_scalper_service.state_engine import feature_names
    from research.pipeline import _features_current

    path = tmp_path / "2026-10-08.parquet"
    assert not _features_current(path)
    names = feature_names(cross=False)
    pq.write_table(pa.table({n: [0.0] for n in names if "300000ms" not in n}), path)
    assert not _features_current(path)
    pq.write_table(pa.table({n: [0.0] for n in names}), path)
    assert _features_current(path)


def _arm(net_usd: float, trades: int = 40, mean_net_bp: float = 2.0, marks: float = 9.0) -> dict:
    return {
        "summary": {"trades": trades, "mean_net_bp": mean_net_bp, "t_stat": 2.5, "win_rate": 0.6},
        "stats": {"net_usd": net_usd, "max_drawdown_pct": 1.0},
        "markouts": {"overall": {"300s": {"n": 50, "mean_bp": marks}}},
        "halts": [],
    }


def test_signal_edge_gate_needs_the_move_at_the_horizon_to_cover_the_round_trip() -> None:
    from research.gates import signal_edge_gate

    assert signal_edge_gate(_arm(1.0, marks=9.0), 300.0, 7.0)["passed"]
    failed = signal_edge_gate(_arm(1.0, marks=3.0), 300.0, 7.0)
    assert not failed["passed"] and failed.get("required", True)
    unmeasured = signal_edge_gate({"markouts": {}}, 300.0, 7.0)
    assert unmeasured["required"] is False and unmeasured["note"] == "not measured"


def test_rule_models_are_gated_and_the_verdict_is_written_once(tmp_path) -> None:
    from ofi_scalper_service.model import ModelError, load_model, write_rules_model
    from ofi_scalper_service.rules import DEFAULT_POLICY, RuleParams
    from research.evaluate_rules import rules_gate_table, write_gates
    from research.gates import passed_all

    agg = {"base": _arm(3.0), "pessimistic": _arm(1.0), "latency_2x": _arm(0.5)}
    gates = rules_gate_table(agg, days=3, horizon_s=300.0, cost_bp=7.0)
    assert passed_all(gates) and gates["min_trades"]["threshold"] == 20  # shallow bars
    agg["base"]["markouts"]["overall"]["300s"]["mean_bp"] = 2.0  # no signal edge
    assert not passed_all(rules_gate_table(agg, days=7, horizon_s=300.0, cost_bp=7.0))

    path = write_rules_model(
        tmp_path, "rules-x", rules=RuleParams().as_dict(), policy=dict(DEFAULT_POLICY)
    )
    with pytest.raises(ModelError, match="ungated"):
        load_model(tmp_path, "rules-x")
    write_gates(path, {"passed": True, "gates": gates})
    model = load_model(tmp_path, "rules-x")  # hash-pinned, and now passing
    assert model.gates.passed and not model.ungated
    with pytest.raises(FileExistsError):
        write_gates(path, {"passed": False})
