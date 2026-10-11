"""Per-symbol core state and the processing order for each closed entry bar.

``SymbolCore.on_close`` follows the spec, on the configured (slow, fast,
entry) ladder (default 1D/4H/1H):

1. Append the entry bar; a fast or slow bar that closed at the same instant
   is appended first.
2. Update swings for every timeframe that received a bar.
3. If fast or slow changed: update trend states, slopes, and rebuild the zones.
4. On a fast close: apply breaks, flips, touches and deletions.
5. Step the retest FSM of every zone within 2 x ATR14(entry) of price, and of
   every FSM mid-retest.
6. Build signals (or rejections) from what armed.

Position management (step 7) is ``manage``, called by the engine for each open
position after ``on_close``.

The caller hands over only closed bars, in time order, and a higher-timeframe
bar only at the entry close where it closes. Nothing here reads a clock, does
I/O or draws random numbers, so the backtester and the live runner get
identical output from identical bars.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from zone_trend.config import Config
from zone_trend.core import exits
from zone_trend.core.indicators import volume_profile_hvn
from zone_trend.core.retest_fsm import RESET_BARS, FsmCtx, RetestFSM
from zone_trend.core.series import BarSeries
from zone_trend.core.signals import build_signal
from zone_trend.core.structure import Leg, StructureState, legs, momentum_decay
from zone_trend.core.swings import SwingTracker
from zone_trend.core.zones import ZoneBook
from zone_trend.types import TF, Bar, Position, Rejected, Side, Signal, Trend

__all__ = ["CloseResult", "SymbolCore"]

PROXIMITY_ATR = 2.0
HVN_BINS = 50
DAY_MS = 86_400_000
# Kept swings alternate high/low, so tails cover every reader with margin:
# trend needs the last 2 highs + 2 lows (4 swings); legs feed slope_ok (last
# impulse + correction) and momentum_decay (last 4 impulses), i.e. ≤ 8 legs.
_TREND_TAIL = 8
_LEGS_TAIL = 16


@dataclass
class CloseResult:
    signals: list[Signal] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)
    zone_events: list[tuple[str, str]] = field(default_factory=list)


class SymbolCore:
    def __init__(self, symbol: str, cfg: Config):
        self.symbol = symbol
        self.cfg = cfg
        self.slow_tf = TF(cfg.timeframes.zones[0])
        self.fast_tf = TF(cfg.timeframes.zones[1])
        self.entry_tf = TF(cfg.timeframes.entry)
        # Entry-bar counts that name wall-clock spans (retest window, time
        # stop, FSM cooldown) scale by entry bars per hour.
        self.entry_scale = TF.H1.ms // self.entry_tf.ms
        ema = cfg.structure.ema_period
        self.series = {tf: BarSeries(tf, ema_period=ema) for tf in TF}
        sw = cfg.swings
        lr = {TF.D1: sw.d1, TF.H4: sw.h4, TF.H1: sw.h1, TF.M15: sw.m15, TF.M5: sw.m5}
        self.swings = {tf: SwingTracker(lr[tf].left, lr[tf].right, sw.k_atr) for tf in TF}
        # ENTERED/FAILED cooldown, kept at 24h of entry bars whatever the entry TF.
        self.reset_bars = RESET_BARS * self.entry_scale
        self.structure = {
            self.slow_tf: StructureState(self.slow_tf),
            self.fast_tf: StructureState(self.fast_tf),
        }
        self.zones = ZoneBook(cfg.zones, self.slow_tf, self.fast_tf)
        self.fsm: dict[str, RetestFSM] = {}
        self.vol_history: list[float] = []  # ATR14(slow) / close, one per slow close
        self.legs_fast: list[Leg] = []
        self.now = -1

    # -- views -------------------------------------------------------------------
    @property
    def trend(self) -> Trend:
        return self.structure[self.fast_tf].trend

    def atr(self, tf: TF) -> float:
        s = self.series[tf]
        return s.last_atr if len(s) else math.nan

    def decay(self, side: Side) -> bool:
        return momentum_decay(self.legs_fast, side, self.cfg.structure.decay_ratio)

    def _last_swing(self, kind: str) -> float | None:
        for sw in reversed(self.swings[self.entry_tf].swings):
            if sw.kind == kind and sw.t_conf <= self.now:
                return sw.price
        return None

    # -- the entry-bar step ------------------------------------------------------
    def on_close(self, entry: Bar, fast: Bar | None = None, slow: Bar | None = None) -> CloseResult:
        for bar in (slow, fast):
            if bar is not None and bar.close_time != entry.close_time:
                raise ValueError("higher-timeframe bars must close with the entry bar they ride on")
        self.now = entry.close_time
        res = CloseResult()
        etf, ftf, stf = self.entry_tf, self.fast_tf, self.slow_tf

        # 1-2: bars and swings
        if slow is not None:
            self._append(stf, slow)
            atr_slow = self.atr(stf)
            if math.isfinite(atr_slow):
                self.vol_history.append(atr_slow / slow.close)
        if fast is not None:
            self._append(ftf, fast)
        prev_close = self.series[etf].last.close if len(self.series[etf]) else entry.open
        self._append(etf, entry)

        # 3: structure and zones
        if slow is not None:
            s = self.series[stf]
            self.structure[stf].update(
                self.swings[stf].tail(_TREND_TAIL), slow.close, self.now, s.ema[-1], None
            )
        if fast is not None:
            s = self.series[ftf]
            sw_fast = self.swings[ftf].tail(_LEGS_TAIL)
            self.structure[ftf].update(
                sw_fast, fast.close, self.now, s.ema[-1], self.structure[stf].trend
            )
            self.legs_fast = legs(sw_fast, ftf.ms, self.atr(stf), fast.close)
        if fast is not None or slow is not None:
            self._rebuild_zones(entry.close)

        # 4: breaks on the fast close
        if fast is not None:
            res.zone_events = self.zones.mark_breaks(
                self.now, fast.high, fast.low, fast.close, self.atr(ftf)
            )

        # 5-6: retest FSMs and signals
        self._step_fsms(entry, prev_close, res)
        return res

    def _append(self, tf: TF, bar: Bar) -> None:
        self.series[tf].append(bar)
        self.swings[tf].update(self.series[tf])

    def _rebuild_zones(self, close: float) -> None:
        fast = self.series[self.fast_tf]
        lookback = self.cfg.zones.hvn_lookback_days * 86_400_000 // self.fast_tf.ms
        window = fast.bars[-lookback:]
        hvn = volume_profile_hvn(
            [b.high for b in window],
            [b.low for b in window],
            [b.volume for b in window],
            bins=HVN_BINS,
        )
        lookback = self.cfg.zones.lookback_days
        self.zones.rebuild(
            self.now,
            self.swings[self.slow_tf].window(self.now, lookback.days(self.slow_tf) * DAY_MS),
            self.swings[self.fast_tf].window(self.now, lookback.days(self.fast_tf) * DAY_MS),
            self.atr(self.slow_tf),
            close,
            hvn,
        )

    def _step_fsms(self, entry: Bar, prev_close: float, res: CloseResult) -> None:
        s = self.series[self.entry_tf]
        atr = s.last_atr
        if not math.isfinite(atr):
            return
        ctx = FsmCtx(
            atr=atr,
            vol_med=s.vol_med[-1],
            prev_close=prev_close,
            trend=self.trend,
            swing_low=self._last_swing("low"),
            swing_high=self._last_swing("high"),
            range_min_strength=self.cfg.zones.range_min_strength,
        )
        near = {
            z.id: z
            for z in self.zones.zones
            if z.lo - PROXIMITY_ATR * atr <= entry.close <= z.hi + PROXIMITY_ATR * atr
        }
        for zid in near:
            if zid not in self.fsm:
                self.fsm[zid] = RetestFSM(zid, self.reset_bars)
        for zid in sorted(self.fsm):
            fsm = self.fsm[zid]
            zone = self.zones.get(zid)
            if zid not in near and fsm.state == "IDLE":
                del self.fsm[zid]
                continue
            armed = fsm.step(entry, zone, ctx, self.cfg.entries, bar_scale=self.entry_scale)
            if armed is None:
                continue
            out = build_signal(
                self.symbol,
                armed,
                zones=self.zones.zones,
                trend=self.trend,
                choch_side=self.structure[self.fast_tf].choch_side,
                legs_fast=self.legs_fast,
                atr_entry=atr,
                atr_fast=self.atr(self.fast_tf),
                vol_history=self.vol_history,
                zone_strength=zone.strength if zone else fsm.strength,
                zone_origin=zone.tf_origin if zone else set(),
                cfg=self.cfg,
            )
            (res.signals if isinstance(out, Signal) else res.rejected).append(out)

    # -- step 7 ------------------------------------------------------------------
    def manage(self, pos: Position) -> list[exits.MoveStop | exits.ExitNextOpen]:
        """Exit decisions for an open position at the current entry close."""
        exits.observe(pos, self.series[self.entry_tf].last)
        return exits.evaluate(
            pos,
            self.atr(self.entry_tf),
            decay=self.decay(pos.side),
            choch_against=self.structure[self.fast_tf].choch_against(pos.side, pos.opened_at),
            fee_taker=self.cfg.exchange.fee_taker,
            cfg=self.cfg.exits,
            bar_scale=self.entry_scale,
        )
