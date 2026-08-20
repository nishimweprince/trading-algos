from __future__ import annotations

import csv
from pathlib import Path
from statistics import mean

import pytest

from metrics import breakeven_tp_rate_required, classify_pair, headline

FIXTURES = Path(__file__).parent / "fixtures"
M15_EXPORT = FIXTURES / "session-hedging-XAUUSD-M15.csv"
H1_EXPORT = FIXTURES / "session-hedging-XAUUSD-H1.csv"
H4_EXPORT = FIXTURES / "session-hedging-XAUUSD-H4.csv"

# Spec §11: bind the metrics to the supplied exports when they are in the workspace. The CSVs are
# user data rather than committed fixtures, so skip instead of failing when they are absent.
requires_exports = pytest.mark.skipif(
    not (M15_EXPORT.is_file() and H1_EXPORT.is_file()),
    reason="M15/H1 export CSVs are not in tests/fixtures",
)


def test_breakeven_formula_matches_v3_export_references() -> None:
    # Spec §0.5: M15 mean lock ≈ −0.875R → 30.4% required; H1 −0.942R → 32.0%.
    assert breakeven_tp_rate_required(-0.875) == pytest.approx(0.3043, abs=0.0005)
    assert breakeven_tp_rate_required(-0.942) == pytest.approx(0.3202, abs=0.0005)


def test_headline_tp_margin_and_outcome_mix() -> None:
    # 10 pairs: 3 TP, 6 lock, 1 whipsaw. mean_loss of the 7 non-TP at −0.9R.
    outcomes = ["tp", "tp", "tp", "lock", "lock", "lock", "lock", "lock", "lock", "whipsaw"]
    rs = [2.0, 2.0, 2.0, -0.9, -0.9, -0.9, -0.9, -0.9, -0.9, -2.0]
    metrics = headline(outcomes=outcomes, r_multiples=rs, concurrent_samples=[1, 2, 3, 2])
    assert metrics.survivor_tp_rate == pytest.approx(0.3)
    assert metrics.outcome_mix.lock == pytest.approx(0.6)
    assert metrics.outcome_mix.whipsaw == pytest.approx(0.1)
    assert metrics.breakeven_tp_rate_required is not None
    assert metrics.tp_rate_margin_pp is not None
    assert metrics.tp_rate_margin_pp_ci_low is not None
    assert metrics.max_concurrent_structures == 3
    assert metrics.median_concurrent == pytest.approx(2.0)


def test_lock_scale_win_is_not_survivor_tp() -> None:
    # Survivor locked +20 pips against a 200-pip stop is a lock, not a target.
    assert (
        classify_pair(
            locked=True,
            same_bar=False,
            long_bucket="win",
            short_bucket="loss",
            pair_r=-0.9,
        )
        == "lock"
    )
    assert (
        classify_pair(
            locked=True,
            same_bar=False,
            long_bucket="win",
            short_bucket="loss",
            pair_r=2.0,
        )
        == "tp"
    )


@requires_exports
def test_metrics_match_m15_and_h1_exports() -> None:
    m15 = _headline_from_export(M15_EXPORT)
    h1 = _headline_from_export(H1_EXPORT)
    m15_lock = _lock_mean_r(M15_EXPORT)
    h1_lock = _lock_mean_r(H1_EXPORT)

    assert m15.n_closed == 66
    assert m15.survivor_tp_rate == pytest.approx(0.288, abs=0.005)
    assert m15.outcome_mix.whipsaw == pytest.approx(0.030, abs=0.005)
    assert m15.outcome_mix.lock == pytest.approx(0.68, abs=0.01)
    assert m15_lock == pytest.approx(-0.875, abs=0.005)
    assert breakeven_tp_rate_required(m15_lock) == pytest.approx(0.304, abs=0.005)

    assert h1.n_closed == 259
    assert h1.survivor_tp_rate == pytest.approx(0.347, abs=0.005)
    assert h1.outcome_mix.whipsaw == pytest.approx(0.031, abs=0.005)
    assert h1.outcome_mix.lock == pytest.approx(0.59, abs=0.04)
    assert h1_lock == pytest.approx(-0.942, abs=0.005)
    assert breakeven_tp_rate_required(h1_lock) == pytest.approx(0.320, abs=0.005)


@pytest.mark.skipif(
    not H4_EXPORT.is_file(), reason="H4 export CSV is not in tests/fixtures"
)
def test_h4_export_is_not_a_regression_target() -> None:
    outcomes, _rs = _load_export_outcomes(H4_EXPORT)
    assert len(outcomes) == 981
    # Broken cash-open anchor. Do not bind metric code to H4 TP/lock rates.


def _headline_from_export(path: Path):
    outcomes, rs = _load_export_outcomes(path)
    return headline(outcomes=outcomes, r_multiples=rs, concurrent_samples=[])


def _lock_mean_r(path: Path) -> float:
    """Mean pair R of lock-at-LOCK_PIPS exits — the §0.5 series, not all non-TP pairs."""
    lock_rs: list[float] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["pair_status"] != "closed":
                continue
            win = _win_pips(row)
            if win is None or abs(win - 20.0) > 1.5:
                continue
            s_pips = _stop_pips(row)
            pair_pips = _num(row["pair_pnl_pips"]) or 0.0
            if s_pips:
                lock_rs.append(pair_pips / s_pips)
    return mean(lock_rs)


def _win_pips(row: dict[str, str]) -> float | None:
    if row["primary_result"] == "win":
        return _num(row["primary_pnl_pips"])
    if row["hedge_result"] == "win":
        return _num(row["hedge_pnl_pips"])
    return None


def _load_export_outcomes(path: Path) -> tuple[list[str], list[float]]:
    outcomes: list[str] = []
    rs: list[float] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            parsed = _pair_from_row(row)
            if parsed is None:
                continue
            kind, pair_r = parsed
            outcomes.append(kind)
            rs.append(pair_r)
    return outcomes, rs


def _pair_from_row(row: dict[str, str]) -> tuple[str, float] | None:
    if row["pair_status"] != "closed":
        return None
    s_pips = _stop_pips(row)
    pair_pips = _num(row["pair_pnl_pips"]) or 0.0
    pair_r = pair_pips / s_pips if s_pips else 0.0
    kind = classify_pair(
        locked=pair_r < 1.5,
        same_bar=row["primary_exit_time"] == row["hedge_exit_time"],
        long_bucket=row["primary_result"] or None,
        short_bucket=row["hedge_result"] or None,
        pair_r=pair_r,
    )
    return kind, pair_r


def _stop_pips(row: dict[str, str]) -> float:
    primary = _num(row["primary_pnl_pips"]) or 0.0
    hedge = _num(row["hedge_pnl_pips"]) or 0.0
    if row["primary_result"] == "loss" and row["hedge_result"] == "loss":
        return max(abs(primary), abs(hedge))
    if row["primary_result"] == "loss":
        return abs(primary)
    if row["hedge_result"] == "loss":
        return abs(hedge)
    return 0.0


def _num(value: str) -> float | None:
    text = (value or "").strip()
    return float(text) if text else None
