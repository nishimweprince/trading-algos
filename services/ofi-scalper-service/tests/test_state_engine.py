"""The feature engine against a hand-computed fixture.

Book: one level per side, tick 0.05. Every expected number below is worked
out in the comment beside it, not produced by the code under test.
"""

from __future__ import annotations

import math

import pytest
from ta_plugin_binance_futures.depth import LocalOrderBook

from ofi_scalper_service.state_engine import (
    GRID_NS,
    EngineConfig,
    LookaheadError,
    MarketState,
    PrefixSeries,
    feature_names,
    funding_bucket,
    level_ofi,
)

MS = 1_000_000
S = 1_000_000_000


def market(**overrides) -> MarketState:
    config = EngineConfig(
        tick_sizes={"BTCUSDT": 0.05, "ETHUSDT": 0.01},
        cross_pairs={"BTCUSDT": "ETHUSDT", "ETHUSDT": "BTCUSDT"},
        **overrides,
    )
    return MarketState(config)


def scripted(state: MarketState) -> LocalOrderBook:
    """BTC events from t=10ms to t=45ms; returns the book as it ends."""
    btc = state["BTCUSDT"]
    book = LocalOrderBook()
    book.load([(100.0, 2.0)], [(100.1, 3.0)])
    btc.on_book(10 * MS, book)  # first state: nothing to diff against
    btc.on_trade(15 * MS, 100.1, 1.0, buyer_is_maker=False)  # aggressive buy 1
    book.apply([(100.0, 5.0)], [])
    btc.on_book(20 * MS, book)  # bid 2 -> 5 at same price: +100*5 - 100*2 = +300
    book.apply([], [(100.05, 1.0)])
    btc.on_book(30 * MS, book)  # new better ask 100.05x1: 0 - 100.05*1 = -100.05
    btc.on_trade(35 * MS, 100.0, 3.0, buyer_is_maker=True)  # aggressive sell 3
    book.apply([(100.0, 0.0), (99.95, 4.0)], [])
    btc.on_book(40 * MS, book)  # best bid falls to 99.95: 0 - 100*5 = -500
    btc.on_funding(45 * MS, 0.0001, 60_000)
    return book


def test_level_ofi_matches_cont() -> None:
    # Bid price up: whole new queue counts, old does not subtract.
    assert level_ofi((100.0, 2.0), (100.1, 1.0), bid=True) == pytest.approx(100.1)
    # Bid price down: old queue removed.
    assert level_ofi((100.0, 2.0), (99.9, 7.0), bid=True) == pytest.approx(-200.0)
    # Ask price down (selling pressure): negative.
    assert level_ofi((100.1, 3.0), (100.0, 1.0), bid=False) == pytest.approx(-100.0)
    # Ask price up (ask removed): positive, the old queue.
    assert level_ofi((100.1, 3.0), (100.2, 9.0), bid=False) == pytest.approx(300.3)
    assert level_ofi(None, (100.0, 1.0), bid=True) == 0.0


def test_hand_computed_fixture() -> None:
    state = market()
    scripted(state)
    f = state.sample("BTCUSDT", 50 * MS)

    # OFI over [-50ms, 50ms): 300 - 100.05 - 500
    assert f["ofi_l1_100ms"] == pytest.approx(-300.05)
    assert f["ofi_l2_100ms"] == 0.0
    assert f["ofi_int_100ms"] == pytest.approx(-300.05)  # default weights: level 1 only

    # Touch 99.95x4 / 100.05x1: mid 100, I = 4/5
    assert f["mid"] == pytest.approx(100.0)
    assert f["imbalance"] == pytest.approx(0.8)
    # microprice = 0.8*100.05 + 0.2*99.95 = 100.03 -> +3 bp
    assert f["microprice_minus_mid_bp"] == pytest.approx(3.0)
    assert f["spread_ticks"] == pytest.approx(2.0)  # 0.10 / 0.05
    assert f["spread_bp"] == pytest.approx(10.0)

    # Trades: +1 @100.1, -3 @100.0 -> TFI = -2/4; VWAP = 400.1/4 = 100.025 -> +2.5 bp
    assert f["tfi_1000ms"] == pytest.approx(-0.5)
    assert f["vwap_to_mid_bp_1000ms"] == pytest.approx(2.5)

    # Depth within 5 bp of 100 = [99.95, 100.05]
    assert f["depth_bid_5bp"] == pytest.approx(399.8)
    assert f["depth_ask_5bp"] == pytest.approx(100.05)

    # Funding at 60s; rate 1 bp -> bucket 0
    assert f["secs_to_funding"] == pytest.approx(59.95)
    assert f["funding_bucket"] == 0

    # Not enough grid history yet
    assert f["rv_10s_bp"] is None and f["ret_30s_bp"] is None


def test_windows_are_half_open() -> None:
    state = market()
    scripted(state)
    # [30ms, 130ms): the t=30 and t=40 events only: -100.05 - 500
    assert state.sample("BTCUSDT", 130 * MS)["ofi_l1_100ms"] == pytest.approx(-600.05)
    # [40ms, 140ms): t=40 only
    assert state.sample("BTCUSDT", 140 * MS)["ofi_l1_100ms"] == pytest.approx(-500.0)


def test_sampling_at_or_before_a_fed_event_is_lookahead() -> None:
    state = market()
    scripted(state)
    with pytest.raises(LookaheadError):
        state.sample("BTCUSDT", 45 * MS)
    with pytest.raises(LookaheadError):
        state.grid(40 * MS)


def test_out_of_order_feed_is_refused() -> None:
    state = market()
    state["BTCUSDT"].on_trade(10 * MS, 100.0, 1.0, False)
    with pytest.raises(ValueError):
        state["BTCUSDT"].on_trade(5 * MS, 100.0, 1.0, False)


def test_cross_asset_ofi_is_lagged() -> None:
    state = market()
    eth = state["ETHUSDT"]
    book = LocalOrderBook()
    book.load([(10.0, 1.0)], [(10.01, 1.0)])
    eth.on_book(10 * MS, book)
    book.apply([(10.0, 3.0)], [])
    eth.on_book(20 * MS, book)  # +10*3 - 10*1 = +20
    # Lag 100ms: at t=110 the window ends at 10ms and misses the t=20 event.
    assert state.sample("BTCUSDT", 110 * MS)["xasset_ofi_l1_100ms"] == 0.0
    assert state.sample("BTCUSDT", 121 * MS)["xasset_ofi_l1_100ms"] == pytest.approx(20.0)


def test_realized_vol_and_trend_use_grid_mids() -> None:
    state = market()
    btc = state["BTCUSDT"]
    book = LocalOrderBook()
    mids = [100.0, 101.0, 100.0]
    for step, mid in enumerate(mids):
        t = step * GRID_NS
        book.load([(mid - 0.05, 1.0)], [(mid + 0.05, 1.0)])
        btc.on_book(t + 1, book)
        state.grid(t + 2)
    f = state.sample("BTCUSDT", 2 * GRID_NS + 3)
    r = math.log(101.0 / 100.0)
    # returns +r, -r: mean 0, sample variance 2r^2 / 1
    assert f["rv_10s_bp"] == pytest.approx(math.sqrt(2) * r * 10_000)

    # 30 s later, mid 102: ret_30s = ln(102/100) against the t=0 grid mid
    t = 30 * S + 10
    book.load([(101.95, 1.0)], [(102.05, 1.0)])
    btc.on_book(t, book)
    out = state.grid(t + 1)["BTCUSDT"]
    assert out["ret_30s_bp"] == pytest.approx(math.log(102.0 / 100.0) * 10_000)
    assert out["ret_120s_bp"] is None


def test_burst_samples_do_not_touch_vol() -> None:
    state = market()
    btc = state["BTCUSDT"]
    book = LocalOrderBook()
    book.load([(99.95, 1.0)], [(100.05, 1.0)])
    btc.on_book(1, book)
    state.grid(2)
    for t in range(3, 50):
        state.sample("BTCUSDT", t)
    assert len(btc.returns) == 0


def test_microprice_table_overrides_weighted_mid() -> None:
    # imbalance 0.8 -> bucket 8; spread 2 ticks -> key (8, 2): +0.5 tick = +0.025
    state = market(microprice_table={(8, 2): 0.5})
    scripted(state)
    f = state.sample("BTCUSDT", 50 * MS)
    assert f["microprice_minus_mid_bp"] == pytest.approx(2.5)


def test_feature_names_cover_the_vector() -> None:
    state = market()
    scripted(state)
    f = state.sample("BTCUSDT", 50 * MS)
    assert set(feature_names()) <= set(f)
    assert len(feature_names()) == len(set(feature_names()))


def test_prefix_series_prune_keeps_window_sums() -> None:
    series = PrefixSeries(1)
    for t in range(100):
        series.append(t, [1.0])
    series.prune(90)
    assert series.window(90, 100) == [10.0]
    assert series.window(0, 100) == [10.0]


def test_funding_bucket_edges() -> None:
    assert funding_bucket(-0.0002) == -2
    assert funding_bucket(-0.00001) == -1
    assert funding_bucket(0.0001) == 0
    assert funding_bucket(0.0003) == 1
    assert funding_bucket(0.001) == 2


def test_grid_clock_emits_every_step_up_to_the_event() -> None:
    from ofi_scalper_service.state_engine import GridClock

    clock = GridClock(100)
    assert clock.due(250) == []  # first event anchors the grid at 300
    assert clock.due(299) == []
    assert clock.due(300) == [300]  # an event at exactly g: sample g before feeding it
    assert clock.due(650) == [400, 500, 600]
