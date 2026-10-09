"""Turn an armed retest into a ``Signal`` (or a ``Rejected`` with its reason).

Symbol-level gates live here; portfolio gates (funding, correlation, BTC gate,
limits, breakers) belong to the risk layer.

- Trend at arming time: Up for longs (Down for shorts), or Range with zone
  strength >= ``range_min_strength``, but never a Range caused by a CHoCH
  against the side ("no new longs").
- In a trend, continuation needs the latest impulse steeper than the latest
  correction, and no momentum decay.
- Volatility: ATR14(1D)/close between the 10th and 95th percentiles of its
  trailing year (at least ``MIN_VOL_HISTORY`` daily values).
- Stop: the retest extreme (``exits.stop_anchor``: or the zone edge, or the
  further of the two) plus 0.3 * ATR14(1H). Skipped when under 0.4 * ATR14(1H),
  over 3 * ATR14(4H), or over ``max_stop_pct``.
- Targets: TP1 at 1.5R; skipped when the next opposing zone is closer than
  1.0R. TP2 is the nearer of that zone's edge and 3R; None when that is not
  beyond TP1 (the runner is then left to the trail).

``entry_ref`` is the arming close; the real entry is the next 1H open, and
sizing re-derives R from the fill.
"""

from __future__ import annotations

import hashlib

import numpy as np

from zone_trend.config import Config
from zone_trend.core.retest_fsm import Armed
from zone_trend.core.structure import Leg, momentum_decay, slope_ok
from zone_trend.types import TF, Rejected, Side, Signal, Trend, Zone

__all__ = ["MIN_VOL_HISTORY", "build_signal", "signal_id", "vol_ok"]

MIN_VOL_HISTORY = 180
VOL_WINDOW = 365


def signal_id(symbol: str, zone_id: str, entry_type: str, bar_time: int) -> str:
    return hashlib.sha1(f"{symbol}|{zone_id}|{entry_type}|{bar_time}".encode()).hexdigest()


def vol_ok(history: list[float], bands: tuple[float, float]) -> tuple[bool, str]:
    """Is the newest ATR/close value inside the trailing-year percentile band?"""
    window = history[-VOL_WINDOW:]
    if len(window) < MIN_VOL_HISTORY:
        return False, "vol_history"
    lo, hi = np.quantile(window, bands)
    return (bool(lo <= window[-1] <= hi), "vol_filter")


def build_signal(
    symbol: str,
    armed: Armed,
    *,
    zones: list[Zone],
    trend: Trend,
    choch_side: Trend | None,
    legs_h4: list[Leg],
    atr_h1: float,
    atr_h4: float,
    vol_history: list[float],
    zone_strength: float,
    zone_origin: set[TF],
    cfg: Config,
) -> Signal | Rejected:
    side = armed.side
    long = side is Side.LONG

    def reject(reason: str, detail: str = "") -> Rejected:
        return Rejected(symbol, side, "B", armed.zone_id, armed.bar_time, reason, detail)

    with_trend = trend == (Trend.UP if long else Trend.DOWN)
    against = Trend.UP if long else Trend.DOWN
    if trend == Trend.RANGE and choch_side == against:
        return reject("choch")
    if not with_trend and not (
        trend == Trend.RANGE and zone_strength >= cfg.zones.range_min_strength
    ):
        return reject("trend", trend.value)
    if with_trend:
        if not slope_ok(legs_h4, side):
            return reject("slope")
        if momentum_decay(legs_h4, side, cfg.structure.decay_ratio):
            return reject("momentum_decay")
    ok, why = vol_ok(vol_history, cfg.filters.vol_percentiles)
    if not ok:
        return reject(why)

    ex = cfg.exits
    entry = armed.close
    buf = ex.stop_buffer_atr * atr_h1
    edge = armed.lo if long else armed.hi
    anchors = {"retest": armed.p, "zone": edge}
    if ex.stop_anchor == "further":
        anchor = min(armed.p, edge) if long else max(armed.p, edge)
    else:
        anchor = anchors[ex.stop_anchor]
    stop = anchor - buf if long else anchor + buf
    dist = (entry - stop) if long else (stop - entry)
    if dist < ex.min_stop_atr_h1 * atr_h1:
        return reject("stop_too_tight", f"{dist / atr_h1:.2f} ATR1h")
    if dist > ex.max_stop_atr_h4 * atr_h4:
        return reject("stop_too_wide_atr", f"{dist / atr_h4:.2f} ATR4h")
    if dist / entry > ex.max_stop_pct:
        return reject("stop_too_wide_pct", f"{dist / entry:.4f}")

    sign = side.sign
    if long:
        opposing = [z.lo for z in zones if z.lo > entry and z.id != armed.zone_id]
        target_zone = min(opposing) if opposing else None
    else:
        opposing = [z.hi for z in zones if z.hi < entry and z.id != armed.zone_id]
        target_zone = max(opposing) if opposing else None
    if target_zone is not None and abs(target_zone - entry) < ex.min_room_r * dist:
        return reject("no_room", f"{abs(target_zone - entry) / dist:.2f}R")
    tp1 = entry + sign * ex.tp1_r * dist
    tp2 = entry + sign * ex.tp2_r * dist
    if target_zone is not None and abs(target_zone - entry) < abs(tp2 - entry):
        tp2 = target_zone
    if sign * (tp2 - tp1) <= 0:
        tp2 = None

    quality = 1
    quality += int(cfg.entries.fib_quality_band[0] <= armed.retr <= cfg.entries.fib_quality_band[1])
    quality += int(zone_strength >= cfg.zones.range_min_strength)
    quality += int(TF.D1 in zone_origin)
    return Signal(
        id=signal_id(symbol, armed.zone_id, "B", armed.bar_time),
        symbol=symbol,
        side=side,
        entry_type="B",
        bar_time=armed.bar_time,
        entry_ref=entry,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        quality=quality,
        zone_id=armed.zone_id,
    )
