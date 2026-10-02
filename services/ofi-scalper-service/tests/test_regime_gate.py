from __future__ import annotations

from ofi_scalper_service.regime_gate import MIN_SPREAD_HISTORY, RegimeGate

S = 1_000_000_000


def gate(**overrides) -> RegimeGate:
    values = {
        "funding_blackout_minutes": 2.0,
        "spread_pctl": 95.0,
        "vol_1m_bp": 5.0,
        "liq_burst_usd": 1_000_000.0,
    }
    values.update(overrides)
    return RegimeGate(**values)


def features(t_s: float, **values) -> dict:
    base = {
        "symbol": "BTCUSDT",
        "t_ns": int(t_s * S),
        "spread_bp": 0.02,
        "rv_60s_bp": 0.5,
        "secs_to_funding": 3600.0,
    }
    base.update(values)
    return base


def test_calm_market_is_on() -> None:
    decision = gate().decide(features(0))
    assert decision.on and decision.threshold_mult == 1.0 and not decision.reasons


def test_funding_blackout_before_and_after() -> None:
    g = gate()
    g.observe(features(0, secs_to_funding=100.0))
    assert g.decide(features(0, secs_to_funding=100.0)).reasons == ("funding_blackout",)
    # Funding passed: the next one is 8h away, but we are 30 s after the last.
    g.observe(features(130, secs_to_funding=8 * 3600 - 30.0))
    decision = g.decide(features(130, secs_to_funding=8 * 3600 - 30.0))
    assert not decision.on and "funding_blackout" in decision.reasons
    g.observe(features(400, secs_to_funding=8 * 3600 - 300.0))
    assert g.decide(features(400)).on


def test_liquidation_burst_is_windowed() -> None:
    g = gate()
    g.on_liquidation("BTCUSDT", 1 * S, 600_000)
    g.on_liquidation("BTCUSDT", 2 * S, 500_000)
    assert "liquidation_burst" in g.decide(features(10)).reasons
    assert g.decide(features(62)).on  # the first one aged out


def test_volatility_switches_off() -> None:
    assert gate().decide(features(0, rv_60s_bp=6.0)).reasons == ("volatility",)


def test_wide_spread_widens_not_stops() -> None:
    g = gate()
    for _ in range(MIN_SPREAD_HISTORY):
        g.observe(features(0, spread_bp=0.02))
    decision = g.decide(features(0, spread_bp=0.5))
    assert decision.on
    assert (decision.threshold_mult, decision.max_inventory_mult) == (1.5, 0.5)


def test_disabled_rules_do_nothing() -> None:
    g = gate(spread_pctl=None, vol_1m_bp=None, liq_burst_usd=None, funding_blackout_minutes=0)
    g.on_liquidation("BTCUSDT", 0, 1e12)
    assert g.decide(features(0, rv_60s_bp=99.0, secs_to_funding=1.0)).on
