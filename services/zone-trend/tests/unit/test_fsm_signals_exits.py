from __future__ import annotations

import pytest

from zone_trend.config import load_config
from zone_trend.core import exits
from zone_trend.core.retest_fsm import ENTERED, FAILED, FsmCtx, RetestFSM, rejection
from zone_trend.core.signals import build_signal
from zone_trend.core.structure import Leg
from zone_trend.types import TF, Bar, Position, Rejected, Side, Signal, Trend, Zone

CFG = load_config()
H = 3_600_000
ZONE = Zone("z1", {TF.D1}, 99.0, 100.0, "resistance", 50.0, 2, 0)
CTX = FsmCtx(
    atr=1.0, vol_med=100.0, prev_close=99.5, trend=Trend.UP, swing_low=94.0, swing_high=None
)


def bar(i: int, o: float, h: float, lo: float, c: float, v: float = 100.0) -> Bar:
    return Bar(i * H, (i + 1) * H - 1, o, h, lo, c, v)


BREAKOUT = bar(0, 99.6, 101.2, 99.5, 101.0, v=200)  # closes 1.0 above hi, body 1.4/1.7
RALLY = bar(1, 101.0, 104.0, 100.8, 103.5)
DIP = bar(2, 103.5, 103.6, 100.6, 101.0)  # back within hi + 0.25 ATR
REJECT = bar(3, 100.6, 101.6, 99.8, 101.5)  # long lower wick, close in top third


def run(bars, zone=ZONE, ctx=CTX):
    fsm = RetestFSM(zone.id)
    out = []
    prev = ctx.prev_close
    for b in bars:
        c = FsmCtx(ctx.atr, ctx.vol_med, prev, ctx.trend, ctx.swing_low, ctx.swing_high)
        out.append(fsm.step(b, zone, c, CFG.entries))
        prev = b.close
    return fsm, out


def test_rejection_candle():
    assert rejection(REJECT, Side.LONG, 0.4, 0.333)
    assert not rejection(RALLY, Side.LONG, 0.4, 0.333)


def test_break_retest_reject_arms_a_long():
    fsm, out = run([BREAKOUT, RALLY, DIP, REJECT])
    armed = out[-1]
    assert armed is not None and armed.side is Side.LONG and fsm.state == ENTERED
    # A = 94 (swing low), B = 104 (highest high), P = 99.8 (lowest low in the retest)
    assert armed.retr == pytest.approx((104 - 99.8) / (104 - 94))
    assert armed.breakout_time == BREAKOUT.close_time


def test_weak_or_quiet_breakouts_do_not_start():
    quiet = bar(0, 99.6, 101.2, 99.5, 101.0, v=120)  # < 1.3 x median
    assert run([quiet])[0].state == "IDLE"
    fsm, _ = run([BREAKOUT], ctx=FsmCtx(1.0, 100.0, 99.5, Trend.DOWN, 94.0, None))
    assert fsm.state == "IDLE"


def test_close_back_through_the_zone_fails():
    collapse = bar(2, 103.5, 103.6, 98.0, 98.5)  # < lo - 0.25 ATR
    fsm, out = run([BREAKOUT, RALLY, collapse, REJECT])
    assert fsm.state == FAILED and not any(out)


def test_timeout_fails_and_reset_after_24_bars():
    drift = [bar(i, 103, 104, 102.5, 103.5) for i in range(1, CFG.entries.retest_window_bars + 2)]
    fsm, _ = run([BREAKOUT, *drift])
    assert fsm.state == FAILED
    fsm2, _ = run([BREAKOUT, *drift, *[bar(100 + i, 103, 104, 102.5, 103.5) for i in range(24)]])
    assert fsm2.state == "IDLE"


def armed():
    return run([BREAKOUT, RALLY, DIP, REJECT])[1][-1]


SIGNAL_KW = dict(
    zones=[ZONE],
    trend=Trend.UP,
    choch_side=None,
    legs_h4=[Leg(True, 2.0), Leg(False, -1.0)],
    atr_h1=1.0,
    atr_h4=3.0,
    vol_history=[0.03] * 200,
    zone_strength=50.0,
    zone_origin={TF.D1},
    cfg=CFG,
)


def test_signal_stop_and_targets():
    sig = build_signal("BTCUSDT", armed(), **SIGNAL_KW)
    assert isinstance(sig, Signal)
    assert sig.stop == pytest.approx(99.8 - 0.3)  # retest low minus 0.3 ATR1h
    r = 101.5 - sig.stop
    assert sig.tp1 == pytest.approx(101.5 + 1.5 * r) and sig.tp2 == pytest.approx(101.5 + 3 * r)
    again = build_signal("BTCUSDT", armed(), **SIGNAL_KW)
    assert again.id == sig.id


def test_tp2_is_the_nearer_opposing_zone_and_no_room_rejects():
    r = 101.5 - 99.5
    target = Zone("z2", {TF.H4}, 101.5 + 2.5 * r, 120, "resistance", 50, 1, 0)
    sig = build_signal("BTCUSDT", armed(), **{**SIGNAL_KW, "zones": [ZONE, target]})
    assert sig.tp2 == pytest.approx(target.lo)
    close = Zone("z3", {TF.H4}, 101.5 + 0.5 * r, 120, "resistance", 50, 1, 0)
    rej = build_signal("BTCUSDT", armed(), **{**SIGNAL_KW, "zones": [ZONE, close]})
    assert isinstance(rej, Rejected) and rej.reason == "no_room"


@pytest.mark.parametrize(
    "override, reason",
    [
        ({"atr_h1": 20.0, "atr_h4": 10.0}, "stop_too_tight"),  # 7.7 < 0.4 x 20
        ({"atr_h4": 0.5}, "stop_too_wide_atr"),
        ({"trend": Trend.RANGE, "choch_side": Trend.UP}, "choch"),
        ({"trend": Trend.DOWN}, "trend"),
        ({"legs_h4": [Leg(True, 0.5), Leg(False, -1.0)]}, "slope"),
        ({"vol_history": [0.03] * 100}, "vol_history"),
        ({"vol_history": [0.03] * 199 + [0.5]}, "vol_filter"),
    ],
)
def test_signal_rejections(override, reason):
    out = build_signal("BTCUSDT", armed(), **{**SIGNAL_KW, **override})
    assert isinstance(out, Rejected) and out.reason == reason


def test_range_trade_needs_a_strong_zone():
    weak = build_signal("BTCUSDT", armed(), **{**SIGNAL_KW, "trend": Trend.RANGE})
    strong = build_signal(
        "BTCUSDT", armed(), **{**SIGNAL_KW, "trend": Trend.RANGE, "zone_strength": 80.0}
    )
    assert isinstance(weak, Rejected) and isinstance(strong, Signal)


def position(**kw) -> Position:
    base = dict(
        symbol="BTCUSDT",
        side=Side.LONG,
        signal_id="s",
        entry=100.0,
        stop=98.0,
        stop_initial=98.0,
        tp1=103.0,
        tp2=106.0,
        qty=1.0,
        qty_open=0.6,
        opened_at=0,
    )
    return Position(**{**base, **kw})


def test_exits_breakeven_then_trail_in_steps():
    pos = position(tp1_done=True)
    acts = exits.evaluate(
        pos, 1.0, decay=False, choch_against=False, fee_taker=0.0005, cfg=CFG.exits
    )
    assert acts == [exits.MoveStop(pytest.approx(100.1), "breakeven")]
    pos.stop = 100.1
    exits.observe(pos, bar(1, 100, 104.5, 100, 104))
    acts = exits.evaluate(
        pos, 1.0, decay=False, choch_against=False, fee_taker=0.0005, cfg=CFG.exits
    )
    assert acts == [exits.MoveStop(pytest.approx(101.5), "trail")]
    pos.stop = 101.5
    exits.observe(pos, bar(2, 104, 104.6, 103, 104))  # +0.1: below the 0.25 ATR step
    assert (
        exits.evaluate(pos, 1.0, decay=False, choch_against=False, fee_taker=0.0005, cfg=CFG.exits)
        == []
    )
    tight = exits.evaluate(
        pos, 1.0, decay=True, choch_against=False, fee_taker=0.0005, cfg=CFG.exits
    )
    assert tight == [exits.MoveStop(pytest.approx(102.6), "trail")]


def test_time_stop_and_choch_exit():
    pos = position(bars_open=CFG.exits.time_stop_bars)
    acts = exits.evaluate(pos, 1.0, decay=False, choch_against=False, fee_taker=0.0, cfg=CFG.exits)
    assert acts == [exits.ExitNextOpen("time")]
    pos.best = 101.5  # 0.75R favourable: no time stop
    assert (
        exits.evaluate(pos, 1.0, decay=False, choch_against=False, fee_taker=0.0, cfg=CFG.exits)
        == []
    )
    assert exits.evaluate(
        pos, 1.0, decay=False, choch_against=True, fee_taker=0.0, cfg=CFG.exits
    ) == [exits.ExitNextOpen("choch")]
