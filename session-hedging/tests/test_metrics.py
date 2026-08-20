from __future__ import annotations

import pytest

from metrics import breakeven_tp_rate_required, headline


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
