from datetime import UTC, date, datetime, timedelta

from ipda.candles import Candle
from ipda.ipda_model import NY, IpdaModel, IpdaParams, trading_day

DAY = date(2025, 3, 5)  # trading day; its Asian range is the evening of 03-04


def _local(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY)


def _bar(start: datetime, o: float, h: float, low: float, c: float) -> list[Candle]:
    """Three 1M candles whose 3M aggregate is exactly (o, h, low, c)."""
    s = start.astimezone(UTC)
    return [
        Candle(s, o, o, o, o, 0.0),
        Candle(s + timedelta(minutes=1), o, h, low, o, 0.0),
        Candle(s + timedelta(minutes=2), o, max(o, c), min(o, c), c, 0.0),
    ]


def _seed(model: IpdaModel, high: float = 2100.0, low: float = 1900.0) -> None:
    days = [(DAY - timedelta(days=i), high, low) for i in range(21, 1, -1)]
    days.append((DAY - timedelta(days=1), 2010.0, 1990.0))  # PDH 2010 / PDL 1990
    model.seed_daily(days)


# fmt: off
PRE = [  # 01:45-01:59: swing-high fractal at 1997 confirmed on the 01:57 bar
    (1, 45, 1995.0, 1995.5, 1994.5, 1995.0),
    (1, 48, 1995.0, 1996.0, 1994.6, 1995.5),
    (1, 51, 1995.5, 1997.0, 1995.0, 1996.0),
    (1, 54, 1996.0, 1996.5, 1995.0, 1995.5),
    (1, 57, 1995.5, 1996.0, 1994.8, 1995.0),
]
KZ = [  # London killzone: run below PDL 1990, then displacement through 1997
    (2, 0, 1995.0, 1995.5, 1991.0, 1991.5),
    (2, 3, 1991.5, 1992.0, 1988.0, 1989.0),
    (2, 6, 1989.0, 1992.0, 1988.5, 1991.8),
    (2, 9, 1992.0, 1995.0, 1991.9, 1994.8),
    (2, 12, 1994.8, 1998.5, 1994.0, 1998.0),  # FVG 1992-1994, close breaks 1997
]
# fmt: on


def _run(model: IpdaModel, kz=KZ, hour_shift: int = 0):
    feed: list[Candle] = []
    for hh, mm in ((20, 0), (21, 0)):  # Asian range 1994-1998
        s = _local(DAY - timedelta(days=1), hh, mm).astimezone(UTC)
        feed.append(Candle(s, 1996.0, 1998.0, 1994.0, 1996.0, 0.0))
    for hh, mm, *ohlc in PRE + list(kz):
        feed += _bar(_local(DAY, hh + hour_shift, mm), *ohlc)
    out = [s for c in feed if (s := model.on_minute(c)) is not None]
    return out


def test_trading_day_rolls_at_five_pm_new_york() -> None:
    assert trading_day(_local(date(2025, 3, 4), 16, 59)) == date(2025, 3, 4)
    assert trading_day(_local(date(2025, 3, 4), 17, 0)) == date(2025, 3, 5)


def test_long_setup_after_sweep_shift_and_fvg() -> None:
    model = IpdaModel()
    _seed(model)
    setups = _run(model)
    assert len(setups) == 1
    s = setups[0]
    assert s.direction == "buy"
    assert (s.fvg_bottom, s.fvg_top) == (1992.0, 1994.0)
    assert s.entry == 1993.0
    assert s.stop_loss == 1988.0  # sweep extreme
    assert s.take_profit == 1993.0 + 2 * 5.0
    assert s.equilibrium == 2000.0
    assert s.killzone == "london"
    assert s.created_at == _local(DAY, 2, 15).astimezone(UTC)
    assert s.expires_at == _local(DAY, 5, 0).astimezone(UTC)


def test_no_setup_without_a_sweep() -> None:
    kz = [list(b) for b in KZ]
    kz[1][4] = kz[2][4] = 1990.5  # lows and closes stay above PDL 1990
    kz[1][5] = kz[2][2] = 1990.8
    model = IpdaModel()
    _seed(model)
    assert _run(model, kz) == []
    assert model.stats["sweep_sell_side"] == 0


def test_premium_discount_filter() -> None:
    model = IpdaModel()
    _seed(model, high=2000.0, low=1880.0)  # EQ 1940: a 1993 long is in premium
    assert _run(model) == []
    assert model.stats["reject_pd"] == 1

    unfiltered = IpdaModel(IpdaParams(pd_filter=False))
    _seed(unfiltered, high=2000.0, low=1880.0)
    assert len(_run(unfiltered)) == 1


def test_outside_killzone_produces_nothing() -> None:
    model = IpdaModel()
    _seed(model)
    assert _run(model, hour_shift=4) == []  # 05:45-06:14, between killzones


def test_risk_bounds() -> None:
    model = IpdaModel(IpdaParams(max_risk=4.0))
    _seed(model)
    assert _run(model) == []
    assert model.stats["reject_risk"] == 1


def test_liquidity_target_uses_nearest_pool_at_least_one_r_away() -> None:
    model = IpdaModel(IpdaParams(target_mode="liquidity"))
    _seed(model)
    (s,) = _run(model)
    assert s.take_profit == 1998.0  # Asian high, exactly 1R above the 1993 entry

    farther = IpdaModel(IpdaParams(target_mode="liquidity", min_risk=0.5, stop_buffer=0.5))
    _seed(farther)
    (s,) = _run(farther)
    assert s.take_profit == 2010.0  # risk 5.5: Asian high is under 1R, PDH is the draw


def test_replayed_minutes_are_ignored() -> None:
    model = IpdaModel()
    _seed(model)
    first = _run(model)
    assert len(first) == 1
    assert _run(model) == []  # same minutes again: older than last_minute
