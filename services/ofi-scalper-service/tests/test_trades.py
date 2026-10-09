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
    assert report["gross_bp_mean"] == pytest.approx(0.0, abs=1e-3)  # +8 bp and -8 bp
    assert report["cost_bp_mean"] == pytest.approx(4.0, abs=0.01)  # two maker legs
    assert report["exit_liquidity"] == {"maker": 2}
    low, high = report["net_bp_ci95"]
    assert low < report["mean_net_bp"] < high
    # 10% of 4 bp of fees back with BNB.
    assert report["net_bp_mean_with_bnb"] == pytest.approx(report["mean_net_bp"] + 0.4, abs=0.01)
    assert any("gross 0.0, costs 4.0" in line for line in _trading_lines(report))
    assert any("maker fill 66.7%" in line for line in _trading_lines(report))
    assert trading_summary(tmp_path, date(2026, 9, 22)) is None
    assert summarise([])["trades"] == 0


def test_background_appender_writes_records_off_the_caller(tmp_path) -> None:
    from ofi_scalper_service.trades import BackgroundAppender, JsonlDaily, TradeBook

    writer = BackgroundAppender()
    book = TradeBook(tmp_path, writer=writer)
    t0 = 1_790_000_000_000_000_000
    for i in range(50):
        book.signal(t0 + i, {"i": i})
    writer.flush()
    rows = JsonlDaily(tmp_path / "signals").read("2026-09-21")
    assert [r["i"] for r in rows] == list(range(50)) and writer.dropped == 0
    book.close()


def test_time_to_fill_and_the_fill_rate_each_timeout_would_keep() -> None:
    from ofi_scalper_service.trades import time_to_fill

    fills = []
    for seconds in (0.4, 1.5, 2.5, 4.0):
        c = cycle(60048.0, "take_profit")
        c.entry_fill_ns = T0 + int(seconds * S)
        fills.append(record(c))
    assert [r["fill_ms"] for r in fills] == [400.0, 1500.0, 2500.0, 4000.0]
    unfilled = record(cycle(60048.0, "take_profit"))
    unfilled.update(filled=False, fill_ms=None)
    out = time_to_fill([*fills, unfilled])
    assert out["n"] == 4 and out["max"] == 4000.0
    assert out["fill_rate_within_ms"] == {"1000": 0.2, "2000": 0.4, "3000": 0.6, "5000": 0.8}
    assert summarise(fills)["time_to_fill_ms"]["p50"] == 2500.0
    assert time_to_fill([])["fill_rate_within_ms"]["3000"] is None
    lines = _trading_lines(summarise(fills))
    assert any(line.startswith("time to fill p50/p90") and "3s 75%" in line for line in lines)


def test_signal_marks_measure_each_episode_from_the_signal_sample(tmp_path) -> None:
    from ofi_scalper_service.trades import JsonlDaily, SignalMarks, summarise_markouts

    journal = JsonlDaily(tmp_path / "markouts")
    marks = SignalMarks(journal, horizons_s=(1, 5), episode_s=5)
    marks.on_sample("BTCUSDT", T0, 100.0)
    marks.on_signal(T0, {"symbol": "BTCUSDT", "side": "buy", "action": "skip:halted"}, 100.0)
    # The same side within the episode is not a new signal; the other side is.
    marks.on_signal(T0 + S // 2, {"symbol": "BTCUSDT", "side": "buy", "action": "enter"}, 100.0)
    marks.on_signal(T0 + S // 2, {"symbol": "BTCUSDT", "side": "sell", "action": "enter"}, 100.005)
    for i in range(1, 61):  # 100 ms samples, +0.1 bp each
        marks.on_sample("BTCUSDT", T0 + i * S // 10, 100.0 * (1 + i * 1e-5))
    rows = journal.read("2026-09-21")
    assert [(r["side"], r["action"]) for r in rows] == [("buy", "skip"), ("sell", "enter")]
    assert rows[0]["moves"] == {
        "1s": pytest.approx(1.0, abs=1e-3),
        "5s": pytest.approx(5.0, abs=1e-3),
    }
    assert rows[1]["moves"]["1s"] == pytest.approx(-1.0, abs=1e-3)  # from 100.005 at +0.5 s
    summary = summarise_markouts(rows)
    assert summary["episodes"] == 2 and summary["by_action"]["skip"]["5s"]["n"] == 1


def test_signal_marks_break_on_a_sample_gap_and_flush_on_close(tmp_path) -> None:
    from ofi_scalper_service.trades import JsonlDaily

    book = TradeBook(tmp_path)
    book.marks.on_sample("ETHUSDT", T0, 10.0)
    book.marks.on_signal(T0, {"symbol": "ETHUSDT", "side": "sell", "action": "enter"}, 10.0)
    book.marks.on_sample("ETHUSDT", T0 + S // 10, 10.0)
    book.marks.on_sample("ETHUSDT", T0 + 3 * S, 9.0)  # a 2.9 s pause: no move across it
    book.marks.on_signal(T0 + 4 * S, {"symbol": "ETHUSDT", "side": "buy", "action": "enter"}, 9.0)
    book.close()  # the open one is written with what it has
    rows = JsonlDaily(tmp_path / "markouts").read("2026-09-21")
    assert len(rows) == 2 and rows[0]["moves"]["0.1s"] == 0.0
    assert rows[0]["moves"]["1s"] is None and rows[1]["moves"]["0.1s"] is None
