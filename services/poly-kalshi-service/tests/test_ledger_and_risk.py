from __future__ import annotations

from pathlib import Path

import pytest
from conftest import NS, OPEN

from poly_kalshi_service.ledger import Ledger, Position
from poly_kalshi_service.risk import RiskLimits, RiskState


def _position(open_unix: int, side: str = "yes", shares: float = 100, px: float = 0.5) -> Position:
    return Position("BTC", open_unix, side, shares, shares * px, 0.07 * px * (1 - px) * shares, 0)


def test_cash_survives_300_windows(tmp_path: Path) -> None:
    """kpc trap 4: a ledger that never credits payouts drains in ~290 entries."""
    ledger = Ledger.open(tmp_path / "ledger.json", 1000.0)
    for i in range(300):
        open_unix = OPEN + 900 * i
        position = _position(open_unix)
        assert ledger.can_afford(position.cost + position.fee)
        ledger.buy(position)
        ledger.settle("BTC", open_unix, "yes" if i % 2 else "no", (open_unix + 960) * NS)
    fees = 300 * 0.07 * 0.25 * 100
    assert ledger.cash == pytest.approx(1000 - fees)
    assert not ledger.open_positions()


def test_settle_is_idempotent_and_loss_pays_nothing(tmp_path: Path) -> None:
    ledger = Ledger.open(None, 100.0)
    ledger.buy(_position(OPEN, "no"))
    first = ledger.settle("BTC", OPEN, "yes", OPEN * NS)
    assert first is not None and first.payout == 0.0
    assert ledger.settle("BTC", OPEN, "yes", OPEN * NS) is None
    assert ledger.cash == pytest.approx(100 - 50 - 1.75)


def test_one_position_per_window() -> None:
    ledger = Ledger.open(None, 1000.0)
    ledger.buy(_position(OPEN))
    with pytest.raises(ValueError, match="second position"):
        ledger.buy(_position(OPEN))


def test_ledger_persists_open_positions(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = Ledger.open(path, 1000.0)
    ledger.buy(_position(OPEN))
    again = Ledger.open(path, 5.0)
    assert again.cash == pytest.approx(ledger.cash)
    assert len(again.open_positions()) == 1
    again.settle("BTC", OPEN, "yes", OPEN * NS)
    assert Ledger.open(path, 5.0).cash == pytest.approx(ledger.cash + 100)


def test_risk_limits(tmp_path: Path) -> None:
    kill = tmp_path / "KILL"
    risk = RiskState(RiskLimits(max_open_notional_usd=120, daily_loss_limit_usd=10, kill_file=kill))
    ledger = Ledger.open(None, 1000.0)
    t = OPEN * NS
    assert risk.check(ledger, 50, t) is None
    ledger.buy(_position(OPEN))  # 51.75 open
    assert risk.check(ledger, 80, t) == "max_open_notional"
    ledger.settle("BTC", OPEN, "no", t)  # -51.75 today
    assert risk.check(ledger, 50, t) == "daily_loss_limit"
    assert risk.check(ledger, 50, t + 86_400 * NS) is None  # a new UTC day
    assert risk.check(Ledger.open(None, 10.0), 50, t) == "insufficient_cash"
    kill.touch()
    assert risk.check(ledger, 1, t) == "halted"
    assert risk.ack() == (False, f"kill file {kill} still exists")
    kill.unlink()
    assert risk.ack() == (True, "cleared")
