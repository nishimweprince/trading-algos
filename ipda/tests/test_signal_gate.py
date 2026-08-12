from __future__ import annotations

from datetime import UTC, datetime

from ipda.signal_gate import SignalGate, signal_id_for


def test_fire_once_then_lock() -> None:
    gate = SignalGate()
    bucket = datetime(2026, 1, 1, 0, 3, tzinfo=UTC)
    assert gate.is_locked("EURUSD", bucket) is False
    gate.lock("EURUSD", bucket)
    assert gate.is_locked("EURUSD", bucket) is True
    assert gate.is_locked("EURUSD", datetime(2026, 1, 1, 0, 6, tzinfo=UTC)) is False


def test_lock_is_per_symbol() -> None:
    gate = SignalGate()
    bucket = datetime(2026, 1, 1, 0, 3, tzinfo=UTC)
    gate.lock("XAUUSD", bucket)
    assert gate.is_locked("XAUUSD", bucket) is True
    assert gate.is_locked("BTCUSD", bucket) is False


def test_signal_id_is_deterministic_and_direction_sensitive() -> None:
    bucket = datetime(2026, 1, 1, 0, 3, tzinfo=UTC)
    a = signal_id_for("EURUSD", bucket, "buy")
    b = signal_id_for("EURUSD", bucket, "buy")
    c = signal_id_for("EURUSD", bucket, "sell")
    assert a == b
    assert a != c


def test_lock_set_stays_bounded() -> None:
    gate = SignalGate()
    for i in range(1000):
        gate.lock(
            "EURUSD",
            datetime(2026, 1, 1, tzinfo=UTC).replace(microsecond=i % 999999 + 1),
        )
    assert len(gate._locked) <= 512
