"""Offline tests: trade simulation, grouping/reposts, image-clock dating, page parsing."""
from datetime import UTC, datetime

import channel
import pandas as pd
import signals
from backtest import Rules, simulate

PIP = 0.1


def bars(rows, spread=0.2, start="2026-11-10 08:00"):
    """rows: (open, high, low, close) bid prices, one per minute."""
    idx = pd.date_range(start, periods=len(rows), freq="1min")
    df = pd.DataFrame(rows, columns=["bo", "bh", "bl", "bc"], index=idx)
    for c in ("o", "h", "l", "c"):
        df["a" + c] = df["b" + c] + spread
    return df


R = Rules(sl=50, tp=100, trail_start=0, trail_dist=0, trail_step=0, max_hold_h=24)
T0 = pd.Timestamp("2026-11-10 08:00")


def test_buy_hits_tp_at_ask_entry():
    b = bars([(100, 100.5, 99.8, 100.2), (100.2, 110.5, 100.1, 110)])
    r = simulate(b, T0, "BUY", R, PIP)
    assert r["entry"] == 100.2 and r["reason"] == "tp" and r["pips"] == 100


def test_same_bar_sl_and_tp_takes_sl():
    b = bars([(100, 100, 100, 100), (100, 115, 90, 100)])
    r = simulate(b, T0, "BUY", R, PIP)
    assert r["reason"] == "sl" and r["pips"] == -50


def test_sell_exits_on_ask():
    # sell at bid 100; stop 105 is hit when ask (bid high + 0.2) reaches it
    b = bars([(100, 100, 100, 100), (100, 104.9, 99.9, 104)])
    r = simulate(b, T0, "SELL", R, PIP)
    assert r["reason"] == "sl" and r["exit"] == 105.0


def test_trailing_locks_profit():
    rules = Rules(sl=50, tp=1000, trail_start=50, trail_dist=30, trail_step=10, max_hold_h=24)
    b = bars([(100, 100, 100, 100), (100, 108, 100, 108), (108, 108, 104, 104)], spread=0)
    r = simulate(b, T0, "BUY", rules, PIP)
    # best 108 -> stop 105 (80 pips past start, step ok); next bar trades 104 -> trail exit
    assert r["reason"] == "trail" and r["pips"] == 50


def test_market_closed_is_skipped():
    b = bars([(100, 100, 100, 100)], start="2026-11-10 10:00")
    assert simulate(b, T0, "BUY", R, PIP)["status"] == "market_closed"


def _post(i, t, **llm):
    base = {"kind": "signal", "ticker": "XAUUSD", "direction": "BUY", "entry_low": None,
            "entry_high": None, "entry_price": None, "current_price": None,
            "pips_in_profit": None, "stop_loss": None, "take_profit": None,
            "image_clock": None, "confidence": 0.9, "note": ""}
    base.update(llm)
    return {"post": {"id": i, "time": t}, "llm": base}


def test_grouping_keeps_earliest_and_flags_reposts():
    rows = [
        _post(1, "2026-11-10T06:00:00+00:00", entry_low=4115, entry_high=4121),
        _post(2, "2026-11-10T06:10:00+00:00", entry_price=4120.39, current_price=4125.17),
        _post(3, "2026-11-10T07:00:00+00:00", direction="SELL", entry_price=4130.0),
        _post(4, "2026-11-12T07:00:00+00:00", entry_price=4120.39, current_price=4140),
        _post(5, "2026-11-12T08:00:00+00:00", kind="promo"),
    ]
    ideas = signals.build(rows)
    assert [i.first_post_id for i in ideas] == [1, 3, 4]
    assert ideas[0].post_ids == [1, 2] and ideas[0].stated_entry == 4118
    assert ideas[2].repost and not ideas[0].repost
    assert ideas[2].pips_at_first_sight == 196.1


def test_missing_ticker_defaults_to_gold():
    ideas = signals.build([_post(1, "2026-11-10T06:00:00+00:00", ticker=None)])
    assert ideas[0].ticker == "XAUUSD"


def test_image_clock_picks_nearest_zone():
    post = datetime(2026, 11, 10, 5, 57, tzinfo=UTC)
    assert signals.clock_to_utc("13:55", post) == datetime(2026, 11, 10, 5, 55,
                                                           tzinfo=UTC)
    post = datetime(2026, 11, 9, 8, 7, tzinfo=UTC)
    assert signals.clock_to_utc("12:02", post) == datetime(2026, 11, 9, 8, 2,
                                                           tzinfo=UTC)


def test_parse_page():
    page = (
        '<div class="tgme_widget_message_wrap"><div data-post="callistofx/42">'
        "<a class=\"tgme_widget_message_photo_wrap\" style=\"background-image:url('https://x/y.jpg')\">"
        '</a><div class="tgme_widget_message_text js-message_text">BUY <b>NOW</b><br/>gold</div>'
        '<time datetime="2026-11-10T05:57:00+00:00">05:57</time></div></div>'
    )
    (p,) = channel.parse_page(page, "callistofx")
    assert p.id == 42 and p.text == "BUY NOW\ngold" and p.photos == ["https://x/y.jpg"]
