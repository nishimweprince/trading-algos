from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import (
    OPEN,
    at,
    ctl,
    kalshi_delta,
    kalshi_snapshot,
    make_engine,
    poly_book,
    poly_change,
    setup_window,
    window,
)

from poly_kalshi_service.engine import Engine
from poly_kalshi_service.trades import read_journal


def _journal(tmp_path: Path, kind: str) -> list[dict]:
    return [r for r in read_journal(tmp_path / "journal") if r["type"] == kind]


def _tick(engine: Engine, w, sec: float) -> int:
    """A Kalshi delta far from the touch at ``sec``: drives evaluation and pending fills."""
    t = at(w.open_unix, sec)
    engine.handle_line(t, "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1.0))
    return t


def test_kalshi_no_bids_become_yes_asks(engine: Engine) -> None:
    w = window()
    setup_window(engine, w)
    k, y, n = engine.quotes("BTC")
    assert (k.bid, k.ask) == (0.70, 0.72)
    assert k.mid == pytest.approx(0.71)
    engine.handle_line(at(OPEN, 1), "k", kalshi_delta(w.kalshi_ticker, "no", 0.29, 10, seq=2))
    assert engine.quotes("BTC")[0].ask == 0.71
    engine.handle_line(at(OPEN, 1.1), "k", kalshi_delta(w.kalshi_ticker, "no", 0.29, -10, seq=3))
    assert engine.quotes("BTC")[0].ask == 0.72


def test_poly_size_zero_removes_the_level(engine: Engine) -> None:
    w = window()
    setup_window(engine, w)
    engine.handle_line(at(OPEN, 1), "p", poly_change(w.yes_token, 0.46, 0, "SELL"))
    assert engine.quotes("BTC")[1].ask == 0.47
    engine.handle_line(at(OPEN, 1.1), "p", poly_change(w.yes_token, 0.47, 0, "SELL"))
    assert not engine.quotes("BTC")[1].valid  # an empty side is invalid, not stale


def test_bbo_mismatch_is_counted(engine: Engine) -> None:
    w = window()
    setup_window(engine, w)
    change = poly_change(w.yes_token, 0.43, 5, "BUY", best_bid="0.44", best_ask="0.46")
    engine.handle_line(at(OPEN, 1), "p", change)
    bad = poly_change(w.yes_token, 0.42, 5, "BUY", best_bid="0.45", best_ask="0.46")
    engine.handle_line(at(OPEN, 1.1), "p", bad)
    assert engine.counters["bbo_checks_pc"] == 2
    assert engine.counters["bbo_mismatch_pc"] == 1


def test_no_decision_before_entry_early_then_first_crossing_enters(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    w = window()
    setup_window(engine, w)  # the gap is already 0.26 at 0.5 s
    assert not engine.state["BTC"].traded
    t = _tick(engine, w, 20.0)
    assert engine.state["BTC"].traded
    (decision,) = _journal(tmp_path, "decision")
    assert decision["action"] == "enter"
    assert decision["side"] == "yes"
    assert decision["entry_px"] == 0.46
    assert decision["contracts"] == 108
    assert decision["blocked"] is None
    assert len(engine.pending) == 1
    # Not filled before the latency has passed...
    engine.handle_line(t + 100_000_000, "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1))
    assert len(engine.pending) == 1
    # ...and filled at the book as it stands 150 ms later. The limit is the touch
    # (0.46), so the 0.47 level is out of reach: 60 of 108, the rest cancelled.
    engine.handle_line(t + 150_000_000, "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1))
    assert not engine.pending
    (fill,) = _journal(tmp_path, "fill")
    assert fill["shares"] == 60
    assert fill["ordered"] == 108
    assert fill["levels"] == 1
    assert fill["vwap"] == 0.46
    assert fill["touch_px"] == 0.46
    assert fill["fee"] == pytest.approx(0.07 * 60 * 0.46 * 0.54, abs=1e-4)
    assert engine.ledger.cash == pytest.approx(1000 - fill["cost"] - fill["fee"], abs=1e-3)


def test_one_decision_per_window(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    w = window()
    setup_window(engine, w)
    for sec in (20, 30, 40):
        _tick(engine, w, sec)
    assert len(_journal(tmp_path, "decision")) == 1


def test_book_moved_away_means_no_fill(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    w = window()
    setup_window(engine, w)
    t = _tick(engine, w, 20.0)
    # Within the latency the asks at 0.46 and 0.47 are taken by someone else.
    engine.handle_line(t + 50_000_000, "p", poly_change(w.yes_token, 0.46, 0, "SELL"))
    engine.handle_line(t + 60_000_000, "p", poly_change(w.yes_token, 0.47, 0, "SELL"))
    engine.handle_line(t + 70_000_000, "p", poly_change(w.yes_token, 0.52, 100, "SELL"))
    engine.handle_line(t + 200_000_000, "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1))
    (fill,) = _journal(tmp_path, "fill")
    assert fill["shares"] == 0
    assert engine.ledger.cash == 1000
    assert not engine.ledger.positions
    assert engine.counters["no_fills"] == 1


def test_fill_walks_levels_that_improve_within_the_latency(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    w = window()
    setup_window(engine, w)
    t = _tick(engine, w, 20.0)
    # A cheaper level appears before the order lands: it is taken first.
    engine.handle_line(t + 10_000_000, "p", poly_change(w.yes_token, 0.45, 30, "SELL"))
    engine.handle_line(t + 200_000_000, "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1))
    (fill,) = _journal(tmp_path, "fill")
    assert fill["shares"] == 90
    assert fill["levels"] == 2
    assert fill["vwap"] == pytest.approx((30 * 0.45 + 60 * 0.46) / 90, abs=1e-4)


def test_skip_consumes_the_window_and_places_nothing(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    w = window()
    t = at(OPEN, 0.5)
    engine.handle_line(t, "c", ctl(w.control()))
    # Kalshi YES 0.01 / 0.02 (mid 0.015); Polymarket YES mid 0.25: gap -0.235.
    engine.handle_line(t + 1, "k", kalshi_snapshot(w.kalshi_ticker, [(0.01, 10)], [(0.98, 500)]))
    engine.handle_line(t + 3, "p", poly_book(w.yes_token, [(0.24, 100)], [(0.26, 100)]))
    engine.handle_line(t + 4, "p", poly_book(w.no_token, [(0.62, 100)], [(0.65, 100)]))
    _tick(engine, w, 25)
    (decision,) = _journal(tmp_path, "decision")
    assert decision["action"] == "skip"
    assert decision["side"] == "no"
    assert decision["entry_px"] == 0.65
    # Later, a cheap price appears: no retry.
    engine.handle_line(at(OPEN, 30), "p", poly_change(w.no_token, 0.55, 100, "SELL"))
    _tick(engine, w, 31)
    assert len(_journal(tmp_path, "decision")) == 1
    assert not engine.pending


def test_ghost_gap_at_the_roll(tmp_path: Path) -> None:
    """The old window's terminal 0.99/0.01 book can never feed the new window."""
    engine = make_engine(tmp_path, params=None)
    old = window(open_unix=OPEN)
    new = window(open_unix=OPEN + 900)
    setup_window(engine, old)
    # The new window is identified and its Polymarket books arrive early.
    engine.handle_line(at(OPEN, 880), "c", ctl(new.control()))
    engine.handle_line(at(OPEN, 881), "p", poly_book(new.yes_token, [(0.44, 300)], [(0.46, 300)]))
    engine.handle_line(at(OPEN, 882), "p", poly_book(new.no_token, [(0.54, 300)], [(0.56, 300)]))
    # The old contract goes terminal near its close.
    engine.handle_line(at(OPEN, 890), "k", kalshi_snapshot(old.kalshi_ticker, [(0.99, 9)], []))
    # Roll. Seconds 900-902 of the old window = 0-2 of the new.
    engine.handle_line(at(OPEN + 900, 1), "p", poly_change(new.yes_token, 0.45, 5, "BUY"))
    assert old.kalshi_ticker not in engine.books
    assert old.yes_token not in engine.books
    k, y, n = engine.quotes("BTC")
    assert not k.valid  # no Kalshi book for the new window yet
    assert y.valid  # updated since the open
    assert not n.valid  # its last update was before the open
    # Past entry_early_s, still nothing: Kalshi has not printed for the new window.
    for sec in (21, 22, 23):
        engine.handle_line(at(OPEN + 900, sec), "p", poly_change(new.yes_token, 0.45, 5, "BUY"))
    assert not engine.state["BTC"].traded
    window_rows = _journal(tmp_path, "window")
    assert window_rows[0]["open_unix"] == OPEN


def test_reconnect_invalidates_that_venue_only(engine: Engine) -> None:
    w = window()
    setup_window(engine, w)
    engine.handle_line(at(OPEN, 2), "c", ctl({"type": "reconnect", "venue": "k"}))
    k, y, n = engine.quotes("BTC")
    assert not k.valid and y.valid and n.valid
    # A delta onto no snapshot is not a book.
    engine.handle_line(at(OPEN, 3), "k", kalshi_delta(w.kalshi_ticker, "yes", 0.7, 5))
    assert not engine.quotes("BTC")[0].valid
    engine.handle_line(at(OPEN, 4), "k", kalshi_snapshot(w.kalshi_ticker, [(0.7, 1)], [(0.2, 1)]))
    assert engine.quotes("BTC")[0].valid


def test_settlement_credits_cash(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    w = window()
    setup_window(engine, w)
    t = _tick(engine, w, 20.0)
    engine.handle_line(t + 200_000_000, "k", kalshi_delta(w.kalshi_ticker, "no", 0.05, 1))
    (fill,) = _journal(tmp_path, "fill")
    after_buy = engine.ledger.cash
    # Polymarket resolves YES through the WS event; Kalshi says NO by poll.
    resolved = {
        "event_type": "market_resolved",
        "market": w.condition_id,
        "winning_asset_id": w.yes_token,
    }
    engine.handle_line(at(OPEN, 960), "p", json.dumps(resolved))
    kalshi = {"type": "resolution", "venue": "kalshi", "asset": "BTC", "open_unix": OPEN}
    engine.handle_line(at(OPEN, 961), "c", ctl(kalshi | {"outcome": "no"}))
    (settle,) = _journal(tmp_path, "settle")
    assert settle["payout"] == 60
    assert engine.ledger.cash == pytest.approx(after_buy + 60)
    assert settle["pnl"] == pytest.approx(60 - fill["cost"] - fill["fee"], abs=1e-3)
    assert engine.resolutions[("BTC", OPEN)] == {"poly": "yes", "kalshi": "no"}
    # Idempotent: a second resolution (the Gamma poll) changes nothing.
    poly = {"type": "resolution", "venue": "poly", "asset": "BTC", "open_unix": OPEN}
    engine.handle_line(at(OPEN, 970), "c", ctl(poly | {"outcome": "yes"}))
    assert engine.ledger.cash == pytest.approx(after_buy + 60)
    assert len(_journal(tmp_path, "settle")) == 1


def test_mode_off_logs_the_decision_but_places_nothing(tmp_path: Path) -> None:
    engine = make_engine(tmp_path, paper=False)
    w = window()
    setup_window(engine, w)
    _tick(engine, w, 20.0)
    (decision,) = _journal(tmp_path, "decision")
    assert decision["blocked"] == "mode_off"
    assert not engine.pending


def test_risk_blocks_entries_but_not_decisions(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    engine.risk.kill("test")
    w = window()
    setup_window(engine, w)
    _tick(engine, w, 20.0)
    (decision,) = _journal(tmp_path, "decision")
    assert decision["blocked"] == "halted"


def test_insufficient_cash_is_rejected_counted_and_alerted(tmp_path: Path) -> None:
    sent: list[str] = []
    engine = make_engine(tmp_path, cash=10.0)
    engine._alert = lambda key, subject, lines: sent.append(key)
    w = window()
    setup_window(engine, w)
    _tick(engine, w, 20.0)
    (decision,) = _journal(tmp_path, "decision")
    assert decision["blocked"] == "insufficient_cash"
    assert engine.ledger.rejects == 1
    assert sent == ["insufficient_cash"]


def test_unknown_tokens_are_ignored(engine: Engine) -> None:
    w = window()
    setup_window(engine, w)
    engine.handle_line(at(OPEN, 1), "p", poly_book("someone-else", [(0.1, 1)], [(0.9, 1)]))
    assert "someone-else" not in engine.books


def test_cancelled_kalshi_level_leaves_no_ghost(engine: Engine) -> None:
    """Float sums of fractional deltas left ~1e-14 ghosts that crossed the book in production."""
    w = window()
    setup_window(engine, w)  # YES bid 0.70, NO bid 0.28 (YES ask 0.72)
    seq = 2
    # A YES bid at 0.999 built from fractional deltas, then fully cancelled out of order.
    sizes = [300.33, 54.37, 12.1, 0.07]
    for d in sizes + [-s for s in reversed(sizes)][::-1]:
        engine.handle_line(
            at(OPEN, 1), "k", kalshi_delta(w.kalshi_ticker, "yes", 0.999, d, seq=seq)
        )
        seq += 1
    book = engine.books[w.kalshi_ticker]
    assert 0.999 not in book.bids
    assert engine.quotes("BTC")[0].bid == 0.70


def test_crossed_book_is_invalid_not_quoted(engine: Engine) -> None:
    w = window()
    setup_window(engine, w)
    book = engine.books[w.kalshi_ticker]
    book.set_level("bid", 0.80, 5, at(OPEN, 2))  # above the 0.72 ask
    assert book.state() == "crossed"
    assert not engine.quotes("BTC")[0].valid


def test_leg_state_explains_an_invalid_quote(engine: Engine) -> None:
    w = window()
    setup_window(engine, w)
    engine.handle_line(at(OPEN, 3), "p", poly_change(w.yes_token, 0.46, 0, "SELL"))
    engine.handle_line(at(OPEN, 3.1), "p", poly_change(w.yes_token, 0.47, 0, "SELL"))
    legs = engine.status()["assets"]["BTC"]["legs"]
    assert legs["poly_yes"] == {"state": "one_sided", "bid": 0.44, "ask": None}
    assert legs["kalshi"]["state"] == "ok"
    engine.handle_line(at(OPEN, 4), "c", ctl({"type": "reconnect", "venue": "k"}))
    assert engine.status()["assets"]["BTC"]["legs"]["kalshi"]["state"] == "no_snapshot"


def test_invalid_leg_states_are_counted(engine: Engine) -> None:
    w = window()
    setup_window(engine, w)
    book = engine.books[w.kalshi_ticker]
    book.set_level("bid", 0.80, 5, at(OPEN, 21))  # crosses the 0.72 ask
    engine.handle_line(at(OPEN, 22), "p", poly_change(w.yes_token, 0.43, 5, "BUY"))
    assert engine.counters["invalid_kalshi_crossed"] == 1
    assert not engine.state["BTC"].traded
