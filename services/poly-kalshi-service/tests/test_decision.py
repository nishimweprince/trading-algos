from __future__ import annotations

import pytest

from poly_kalshi_service.book import Quote
from poly_kalshi_service.decision import Action, StrategyParams, decide
from poly_kalshi_service.fees import fee_bps, taker_fee

P = StrategyParams()
K_HIGH = Quote(True, 0.70, 0.72)  # mid 0.71
K_LOW = Quote(True, 0.20, 0.22)  # mid 0.21
Y_MID = Quote(True, 0.44, 0.46)  # mid 0.45
N_MID = Quote(True, 0.54, 0.56)
BAD = Quote(False)


def test_enter_buys_cheap_yes_when_kalshi_is_higher() -> None:
    d = decide(K_HIGH, Y_MID, N_MID, P, 100, False)
    assert d.action is Action.ENTER
    assert d.buy_yes is True
    assert d.entry_px == 0.46
    assert d.contracts == 108  # floor(50 / 0.46)
    assert d.gap == pytest.approx(0.26)


def test_enter_buys_no_from_the_no_book_when_kalshi_is_lower() -> None:
    d = decide(K_LOW, Y_MID, N_MID, P, 100, False)
    assert d.action is Action.ENTER
    assert d.buy_yes is False
    assert d.entry_px == 0.56  # the NO book's ask, not 1 - YES bid
    assert d.contracts == 89


def test_crossing_above_the_cap_skips_and_consumes_the_window() -> None:
    y = Quote(True, 0.20, 0.22)
    n = Quote(True, 0.62, 0.65)  # NO is the cheap leg here only by direction
    k = Quote(True, 0.00, 0.02)
    d = decide(k, y, n, P, 100, False)
    assert d.action is Action.SKIP
    assert d.reason == "above_cap"
    assert d.consumes_window
    assert d.contracts == 0


def test_cap_one_disables_the_cap() -> None:
    k, y, n = Quote(True, 0.0, 0.02), Quote(True, 0.20, 0.22), Quote(True, 0.62, 0.65)
    d = decide(k, y, n, StrategyParams(max_entry_px=1.0), 100, False)
    assert d.action is Action.ENTER


@pytest.mark.parametrize(
    ("k", "y", "n", "sec", "traded", "reason"),
    [
        (K_HIGH, Y_MID, N_MID, 100, True, "traded"),
        (BAD, Y_MID, N_MID, 100, False, "invalid_leg"),
        (K_HIGH, BAD, N_MID, 100, False, "invalid_leg"),
        (K_LOW, Y_MID, BAD, 100, False, "invalid_leg"),  # buying NO needs the NO book
        (K_HIGH, Y_MID, N_MID, 19.9, False, "too_early"),
        (K_HIGH, Y_MID, N_MID, 840.1, False, "too_late"),
        (Quote(True, 0.63, 0.65), Y_MID, N_MID, 100, False, "below_threshold"),
    ],
)
def test_no_trade(k: Quote, y: Quote, n: Quote, sec: float, traded: bool, reason: str) -> None:
    d = decide(k, y, n, P, sec, traded)
    assert d.action is Action.NONE
    assert d.reason == reason
    assert not d.consumes_window


def test_threshold_is_inclusive() -> None:
    k = Quote(True, 0.64, 0.66)  # mid 0.65, gap exactly 0.20
    assert decide(k, Y_MID, N_MID, P, 100, False).action is Action.ENTER


def test_entry_boundaries_are_inclusive() -> None:
    assert decide(K_HIGH, Y_MID, N_MID, P, 20, False).action is Action.ENTER
    assert decide(K_HIGH, Y_MID, N_MID, P, 840, False).action is Action.ENTER


def test_no_buy_of_a_dead_book() -> None:
    y = Quote(True, 0.0, 1.0)  # ask at 1 is not a price
    k = Quote(True, 0.95, 0.99)
    assert decide(k, y, N_MID, P, 100, False).reason == "bad_price"


def test_fee_curve_matches_the_venue_schedule() -> None:
    assert fee_bps(0.50) == pytest.approx(350)
    assert fee_bps(0.714) == pytest.approx(200, abs=0.5)
    assert fee_bps(0.04) == pytest.approx(672, abs=1)
    assert taker_fee(0.50, 100) == pytest.approx(1.75)
    assert taker_fee(0.30, 100) == pytest.approx(taker_fee(0.70, 100))
    assert taker_fee(0.0, 100) == 0.0
    assert taker_fee(0.5, 100, rate=0.0) == 0.0
