"""Trade records and the daily trading report."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ofi_scalper_service.daily_check import _trading_lines, trading_summary
from ofi_scalper_service.policy import Cycle
from ofi_scalper_service.trades import TradeBook, summarise, trade_record

T0 = 1_790_000_000_000_000_000
S = 1_000_000_000


def cycle(exit_price: float, reason: str, side: str = "buy") -> Cycle:
    c = Cycle(
        cycle_id=f"BTCUSDT-{exit_price}",
        symbol="BTCUSDT",
        side=side,  # type: ignore[arg-type]
        p=0.7,
        threshold=0.6,
        signal_ns=T0,
        entry_qty=Decimal("0.003"),
        entry_price=Decimal("60000.0"),
    )
    c.fills = {"entry": (Decimal("0.003"), 60000.0), "tp": (Decimal("0.003"), exit_price)}
    c.entry_fill_ns, c.exit_ns, c.exit_reason, c.entry_done = T0 + S, T0 + 5 * S, reason, True
    return c


def record(c: Cycle, **kw) -> dict:
    values = {
        "mode": "shadow",
        "model_version": "v1",
        "maker_bp": 2.0,
        "taker_bp": 5.0,
        "liquidity": {"entry": "maker", "tp": "maker"},
        "funding": None,
        "adverse_bp": {"1s": 1.0, "5s": 2.0, "30s": None},
        "rtt_ms": {"entry": 3.0},
    }
    values.update(kw)
    return trade_record(c, **values)


def test_short_pnl_fees_and_funding() -> None:
    c = cycle(59952.0, "take_profit", side="sell")
    r = record(c, funding={"at_ns": T0 + 2 * S, "rate": 0.0001})
    assert r["gross_usd"] == pytest.approx(0.003 * 48)
    # A short receives a positive funding rate.
    assert r["funding_usd"] == pytest.approx(0.003 * 60000 * 0.0001)
    fees = 0.003 * (60000 + 59952) * 2 / 10_000
    assert r["net_usd"] == pytest.approx(0.144 - fees + 0.018, abs=1e-6)
    assert r["net_bp"] == pytest.approx(r["net_usd"] / 180 * 10_000, abs=1e-3)


def test_funding_outside_the_cycle_is_not_charged() -> None:
    r = record(cycle(60048.0, "take_profit"), funding={"at_ns": T0 + 60 * S, "rate": 0.0001})
    assert r["funding_usd"] == 0.0


def test_summary_and_daily_lines(tmp_path) -> None:
    book = TradeBook(tmp_path)
    win = record(cycle(60048.0, "take_profit"))
    loss = record(cycle(59952.0, "stop"))
    unfilled = {"entry_qty_ordered": "0.003", "filled": False, "rtt_ms": {"entry": 5.0}}
    for r in (win, loss, unfilled):
        book.trade(T0, r)
    book.signal(T0, {"action": "enter"})
    day = date(2026, 9, 21)
    report = trading_summary(tmp_path, day)
    assert report is not None
    assert report["entries"] == 3 and report["trades"] == 2
    assert report["maker_fill_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert report["win_rate"] == 0.5 and report["largest_loss_usd"] < 0
    assert report["brier"] == pytest.approx(((0.7 - 1) ** 2 + 0.7**2) / 2, abs=1e-5)
    assert report["adverse_bp_mean"] == {"1s": 1.0, "5s": 2.0, "30s": None}
    assert report["exit_reasons"] == {"take_profit": 1, "stop": 1}
    assert any("maker fill 66.7%" in line for line in _trading_lines(report))
    assert trading_summary(tmp_path, date(2026, 9, 22)) is None
    assert summarise([])["trades"] == 0
