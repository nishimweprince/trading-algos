from __future__ import annotations

from models import EngineParams
from research.hedge_survivor import (
    SURVIVOR_CANDIDATES,
    _params_for,
    candidate_sha256,
    promotion_gate_results,
    survivor_candidate_manifest,
)


def test_candidate_manifest_is_frozen_unique_and_hashed() -> None:
    manifest = survivor_candidate_manifest()
    ids = [str(item["id"]) for item in SURVIVOR_CANDIDATES]

    assert len(SURVIVOR_CANDIDATES) == 10
    assert len(set(ids)) == len(ids)
    assert len(candidate_sha256()) == 64
    assert manifest["candidate_sha256"] == candidate_sha256()
    assert manifest["external_holdout"] == {
        "status": "locked",
        "minimum_years": 3,
        "requires_complete_m1": True,
        "opened": False,
    }


def test_matched_replay_removes_only_portfolio_occupancy_suppression() -> None:
    base = EngineParams(
        dollars_per_pip_per_qty=10,
        spread_pips_per_side=1.5,
        slippage_pips_per_side=1.5,
        commission_pips_per_side=0.35,
    )
    candidate = next(item for item in SURVIVOR_CANDIDATES if item["id"] == "survivor:unlocked")
    portfolio = _params_for(base, candidate, replay="portfolio", stress_costs=False)
    matched = _params_for(base, candidate, replay="matched_opportunity", stress_costs=False)
    stress = _params_for(base, candidate, replay="portfolio", stress_costs=True)

    assert portfolio.risk_mode == "fixed_fractional"
    assert portfolio.risk_pct_per_r == 0.10
    assert portfolio.max_pair_risk_pct == 0.20
    assert matched.one_open_per_session is False
    assert matched.max_concurrent_structures == 0
    assert matched.max_open_risk_pct == 0
    assert stress.spread_pips_per_side == 3
    assert stress.slippage_pips_per_side == 3
    assert stress.commission_pips_per_side == 0.35


def test_promotion_requires_every_predeclared_gate() -> None:
    passing = dict(
        holdout_net_r=1,
        holdout_profit_factor=1.1,
        stress_net_r=0.2,
        stress_profit_factor=1.05,
        positive_fold_fraction=2 / 3,
        bootstrap_lower_r=0.01,
        candidate_drawdown_r=8,
        incumbent_drawdown_r=10,
        dsr_probability=0.95,
        pbo=0.20,
        matched_net_r=0.1,
        max_concentration=0.69,
        m1_complete=True,
        resolver_fallbacks=0,
        configured_costs_nonzero=True,
    )
    assert promotion_gate_results(**passing)["passed"] is True
    assert promotion_gate_results(**(passing | {"stress_net_r": -0.01}))["passed"] is False
    no_costs = promotion_gate_results(**(passing | {"configured_costs_nonzero": False}))
    assert no_costs["passed"] is False
