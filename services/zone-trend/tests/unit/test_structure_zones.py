from __future__ import annotations

import pytest

from zone_trend.config import load_config
from zone_trend.core.fib import in_band, retracement
from zone_trend.core.structure import (
    Leg,
    StructureState,
    legs,
    momentum_decay,
    slope_ok,
    trend_rule,
)
from zone_trend.core.zones import DAY_MS, ZoneBook
from zone_trend.types import TF, Side, Swing, Trend

CFG = load_config()
H4 = TF.H4.ms


def sw(kind: str, price: float, t: int, tf: TF = TF.H4, atr: float = 1.0, body: float = 0.0):
    return Swing(tf, kind, t, price, t + 1, atr, body)


UP = [sw("low", 90, 0), sw("high", 100, 1), sw("low", 95, 2), sw("high", 105, 3)]
DOWN = [sw("high", 110, 0), sw("low", 100, 1), sw("high", 105, 2), sw("low", 95, 3)]


@pytest.mark.parametrize(
    "swings, close, ema, higher, expected",
    [
        (UP, 104, 100, None, Trend.UP),
        (UP, 104, 100, Trend.DOWN, Trend.RANGE),  # 1D says Down
        (UP, 99, 100, None, Trend.RANGE),  # below EMA50
        (DOWN, 96, 100, Trend.RANGE, Trend.DOWN),
        (UP[:3], 104, 100, None, Trend.RANGE),  # not enough swings
    ],
)
def test_trend_rule(swings, close, ema, higher, expected):
    assert trend_rule(swings, close, ema, higher) is expected


def test_choch_forces_range_until_a_newer_swing():
    st = StructureState(TF.H4)
    assert st.update(UP, 104, 1000, 100, None) is Trend.UP
    assert st.update(UP, 94, 2000, 90, None) is Trend.RANGE  # close below the last HL (95)
    assert st.choch_against(Side.LONG, 1500) and not st.choch_against(Side.SHORT, 0)
    assert st.update(UP, 104, 3000, 90, None) is Trend.RANGE  # still overridden
    newer = [*UP[:-1], sw("high", 106, 2500)]  # t_conf 2501 > 2000
    assert st.update(newer, 104, 4000, 90, None) is Trend.UP


def test_slopes_are_normalised_by_daily_volatility():
    # +10% over 6 4H bars (one day) with ATR1D/close = 5% -> slope 2.0
    out = legs([sw("low", 100, 0), sw("high", 110, 6 * H4)], H4, atr_d1=5.0, close=100)
    assert out == [Leg(True, pytest.approx(2.0))]


def test_slope_gate_and_momentum_decay():
    steep = [Leg(True, 2.0), Leg(False, -1.0)] * 3 + [Leg(True, 2.0), Leg(False, -1.0)]
    assert slope_ok(steep, Side.LONG) and not slope_ok(steep, Side.SHORT)
    tired = [*steep, Leg(True, 0.5)]
    assert momentum_decay(tired, Side.LONG, 0.6)
    assert not momentum_decay(steep, Side.LONG, 0.6)


def test_retracement():
    assert retracement(100, 200, 150) == 0.5
    assert retracement(200, 100, 150) == 0.5  # short: A high, B low
    assert retracement(100, 200, 200) == 0.0 and retracement(100, 200, 100) == 1.0
    assert in_band(0.5, (0.382, 0.786)) and not in_band(0.9, (0.382, 0.786))


# -- zones ---------------------------------------------------------------------
NOW = 400 * DAY_MS


def book() -> ZoneBook:
    return ZoneBook(CFG.zones)


def d1(kind, price, days_ago, atr=10.0, body=3.0):
    t = NOW - days_ago * DAY_MS
    return Swing(TF.D1, kind, t, price, t + DAY_MS, atr, body)


def test_zone_width_is_clamped_and_role_follows_position():
    zb = book()
    zones = zb.rebuild(
        NOW, [d1("high", 200, 50, body=1.0), d1("low", 100, 40, body=9.0)], [], 10, 150
    )
    hi_zone = next(z for z in zones if z.hi == 200)
    lo_zone = next(z for z in zones if z.lo == 100)
    assert hi_zone.lo == pytest.approx(197.5)  # body 1 < 0.25 ATR -> 2.5
    assert lo_zone.hi == pytest.approx(106.0)  # body 9 > 0.6 ATR -> 6
    assert hi_zone.role == "resistance" and lo_zone.role == "support"


def test_nearby_zones_merge_and_merged_width_is_capped():
    zb = book()
    # [197, 200], [200, 203], [203, 206], [206, 209] chain-overlap into one 12-wide zone
    seeds = [d1("high", 200 + 3 * i, 30 + 10 * i) for i in range(4)]
    seeds.append(d1("high", 230, 20))  # far away: its own zone
    zones = zb.rebuild(NOW, seeds, [], 10, 150)
    assert len(zones) == 2
    z = zones[0]
    assert z.touches == 4 and z.hi - z.lo == pytest.approx(10.0)  # capped at 1.0 ATR1D
    assert (z.lo + z.hi) / 2 == pytest.approx(203.0)  # around the mean of seed midpoints


def test_old_and_unconfirmed_swings_do_not_seed():
    zb = book()
    future = Swing(TF.D1, "high", NOW - DAY_MS, 300, NOW + 1, 10, 3)
    zones = zb.rebuild(NOW, [d1("high", 200, 400), future], [], 10, 150)
    assert zones == []


def test_break_flips_role_and_strength_rewards_flip():
    zb = book()
    zb.rebuild(NOW, [d1("high", 200, 30)], [], 10, 150)
    before = zb.zones[0].strength
    events = zb.mark_breaks(NOW + 1, 206, 199, 205, atr_h4=4.0)  # 205 > 200 + 1
    assert events == [(zb.zones[0].id, "break_up")]
    assert zb.zones[0].role == "support" and zb.zones[0].flips == 1
    zb.rebuild(NOW + 2, [d1("high", 200, 30)], [], 10, 150)  # state carried over
    assert zb.zones[0].role == "support" and zb.zones[0].strength == pytest.approx(before + 10)


def test_two_opposite_breaks_within_30_days_delete_the_zone():
    zb = book()
    zb.rebuild(NOW, [d1("high", 200, 30)], [], 10, 150)
    zb.mark_breaks(NOW + 1, 206, 199, 205, atr_h4=4.0)
    events = zb.mark_breaks(NOW + 5 * DAY_MS, 199, 190, 191, atr_h4=4.0)
    assert ("deleted" in [e for _, e in events]) and zb.zones == []
    assert zb.rebuild(NOW + 6 * DAY_MS, [d1("high", 200, 30)], [], 10, 150) == []


def test_three_deep_retests_delete_the_zone():
    zb = book()
    zb.rebuild(NOW, [d1("low", 100, 30)], [], 10, 150)  # support [100, 103]
    for i in range(3):
        zb.mark_breaks(NOW + 2 * i, 104, 100.5, 103.5, atr_h4=4.0)  # deep visit, no break
        zb.mark_breaks(NOW + 2 * i + 1, 110, 105, 108, atr_h4=4.0)  # leave
    assert zb.zones == []


def test_shallow_visits_count_as_touches():
    zb = book()
    zb.rebuild(NOW, [d1("low", 100, 30)], [], 10, 150)
    zb.mark_breaks(NOW + 1, 106, 102.5, 105, atr_h4=4.0)
    zb.mark_breaks(NOW + 2, 110, 105, 108, atr_h4=4.0)
    assert zb.zones[0].extra_touches == 1 and zb.zones[0].deep_retests == 0


def test_keep_strongest_per_side():
    zb = book()
    seeds = [d1("high", 200 + 20 * i, 30 + i) for i in range(8)]
    seeds += [d1("low", 100 - 20 * i, 30 + i) for i in range(4)]
    zones = zb.rebuild(NOW, seeds, [], 10, 150)
    assert sum(z.lo > 150 for z in zones) == CFG.zones.keep_per_side
    assert sum(z.hi < 150 for z in zones) == 4
