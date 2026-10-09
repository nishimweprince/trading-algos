"""The flow guard: which sides short-window flow runs against."""

from __future__ import annotations

from ofi_scalper_service.flow_guard import FlowGuard
from ofi_scalper_service.policy import PolicyParams

S = 1_000_000_000
G = 100_000_000
BASE = {"horizon_s": 300, "threshold": 0.5, "barrier_bp": 20}


def sample(t_ns: int, ofi: float, tfi: float = 0.0, imbalance: float = 0.5) -> dict:
    return {
        "symbol": "BTCUSDT",
        "t_ns": t_ns,
        "ofi_int_1000ms": ofi,
        "tfi_1000ms": tfi,
        "imbalance": imbalance,
        "trigger": "grid",
    }


def warmed(guard: FlowGuard) -> int:
    """Six minutes of small alternating flow; returns the next time."""
    t = 0
    for i in range(3600):
        t = i * G
        assert guard.against(sample(t, 1.0 if i % 2 else -1.0)) == frozenset()
    return t + G


def test_off_without_guard_z() -> None:
    guard = FlowGuard(PolicyParams(**BASE))
    assert not guard.enabled
    assert guard.against(sample(0, -1e9, -1.0, 0.0)) == frozenset()


def test_heavy_selling_is_against_bids_and_buying_against_asks() -> None:
    guard = FlowGuard(PolicyParams(**BASE, guard_z=3.0, guard_tfi=0.5, guard_imb=0.7))
    t = warmed(guard)
    assert guard.against(sample(t, -50.0, -0.9, 0.2)) == frozenset({"buy"})
    assert guard.against(sample(t + G, 50.0, 0.9, 0.8)) == frozenset({"sell"})
    # Every configured condition must hold.
    assert guard.against(sample(t + 2 * G, -50.0, -0.1, 0.2)) == frozenset()  # trade flow mild
    assert guard.against(sample(t + 3 * G, -50.0, -0.9, 0.5)) == frozenset()  # book balanced
    assert guard.flagged == {"buy": 1, "sell": 1}


def test_quiet_until_warm() -> None:
    guard = FlowGuard(PolicyParams(**BASE, guard_z=3.0))
    for i in range(10):
        guard.against(sample(i * G, 1.0 if i % 2 else -1.0))
    assert guard.against(sample(10 * G, -50.0)) == frozenset()
