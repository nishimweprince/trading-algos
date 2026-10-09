"""Per-symbol core state and the processing order for each closed 1H bar.

``SymbolCore.on_close`` follows the spec:

1. Append the 1H bar; a 4H or 1D bar that closed at the same instant is
   appended first.
2. Update swings for every timeframe that received a bar.
3. If 4H or 1D changed: update trend states, slopes, and rebuild the zones.
4. On a 4H close: apply breaks, flips, touches and deletions.
5. Step the retest FSM of every zone within 2 x ATR14(1H) of price, and of every
   FSM mid-retest.
6. Build signals (or rejections) from what armed.

Position management (step 7) is ``manage``, called by the engine for each open
position after ``on_close``.

The caller hands over only closed bars, in time order, and a higher-timeframe
bar only at the 1H close where it closes. Nothing here reads a clock, does I/O
or draws random numbers, so the backtester and the live runner get identical
output from identical bars.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from zone_trend.config import Config
from zone_trend.core import exits
from zone_trend.core.indicators import volume_profile_hvn
from zone_trend.core.retest_fsm import FsmCtx, RetestFSM
from zone_trend.core.series import BarSeries
from zone_trend.core.signals import build_signal
from zone_trend.core.structure import Leg, StructureState, legs, momentum_decay
from zone_trend.core.swings import SwingTracker
from zone_trend.core.zones import ZoneBook
from zone_trend.types import TF, Bar, Position, Rejected, Side, Signal, Trend

__all__ = ["CloseResult", "SymbolCore"]

PROXIMITY_ATR = 2.0
HVN_BINS = 50


@dataclass
class CloseResult:
    signals: list[Signal] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)
    zone_events: list[tuple[str, str]] = field(default_factory=list)


class SymbolCore:
    def __init__(self, symbol: str, cfg: Config):
        self.symbol = symbol
        self.cfg = cfg
        ema = cfg.structure.ema_period
        self.series = {tf: BarSeries(tf, ema_period=ema) for tf in TF}
        sw = cfg.swings
        lr = {TF.D1: sw.d1, TF.H4: sw.h4, TF.H1: sw.h1}
        self.swings = {tf: SwingTracker(lr[tf].left, lr[tf].right, sw.k_atr) for tf in TF}
        self.structure = {TF.D1: StructureState(TF.D1), TF.H4: StructureState(TF.H4)}
        self.zones = ZoneBook(cfg.zones)
        self.fsm: dict[str, RetestFSM] = {}
        self.vol_history: list[float] = []  # ATR14(1D) / close, one per 1D close
        self.legs_h4: list[Leg] = []
        self.now = -1

    # -- views -------------------------------------------------------------------
    @property
    def trend(self) -> Trend:
        return self.structure[TF.H4].trend

    def atr(self, tf: TF) -> float:
        s = self.series[tf]
        return s.last_atr if len(s) else math.nan

    def decay(self, side: Side) -> bool:
        return momentum_decay(self.legs_h4, side, self.cfg.structure.decay_ratio)

    def _last_swing(self, kind: str) -> float | None:
        for sw in reversed(self.swings[TF.H1].swings):
            if sw.kind == kind and sw.t_conf <= self.now:
                return sw.price
        return None

    # -- the hourly step ---------------------------------------------------------
    def on_close(self, h1: Bar, h4: Bar | None = None, d1: Bar | None = None) -> CloseResult:
        for bar in (d1, h4):
            if bar is not None and bar.close_time != h1.close_time:
                raise ValueError("higher-timeframe bars must close with the 1H bar they ride on")
        self.now = h1.close_time
        res = CloseResult()

        # 1-2: bars and swings
        if d1 is not None:
            self._append(TF.D1, d1)
            atr_d1 = self.atr(TF.D1)
            if math.isfinite(atr_d1):
                self.vol_history.append(atr_d1 / d1.close)
        if h4 is not None:
            self._append(TF.H4, h4)
        prev_close = self.series[TF.H1].last.close if len(self.series[TF.H1]) else h1.open
        self._append(TF.H1, h1)

        # 3: structure and zones
        if d1 is not None:
            s = self.series[TF.D1]
            self.structure[TF.D1].update(
                self.swings[TF.D1].visible(self.now), d1.close, self.now, s.ema[-1], None
            )
        if h4 is not None:
            s = self.series[TF.H4]
            sw_h4 = self.swings[TF.H4].visible(self.now)
            self.structure[TF.H4].update(
                sw_h4, h4.close, self.now, s.ema[-1], self.structure[TF.D1].trend
            )
            self.legs_h4 = legs(sw_h4, TF.H4.ms, self.atr(TF.D1), h4.close)
        if h4 is not None or d1 is not None:
            self._rebuild_zones(h1.close)

        # 4: breaks on the 4H close
        if h4 is not None:
            res.zone_events = self.zones.mark_breaks(
                self.now, h4.high, h4.low, h4.close, self.atr(TF.H4)
            )

        # 5-6: retest FSMs and signals
        self._step_fsms(h1, prev_close, res)
        return res

    def _append(self, tf: TF, bar: Bar) -> None:
        self.series[tf].append(bar)
        self.swings[tf].update(self.series[tf])

    def _rebuild_zones(self, close: float) -> None:
        h4 = self.series[TF.H4]
        lookback = self.cfg.zones.hvn_lookback_days * 6
        window = h4.bars[-lookback:]
        hvn = volume_profile_hvn(
            [b.high for b in window],
            [b.low for b in window],
            [b.volume for b in window],
            bins=HVN_BINS,
        )
        self.zones.rebuild(
            self.now,
            self.swings[TF.D1].visible(self.now),
            self.swings[TF.H4].visible(self.now),
            self.atr(TF.D1),
            close,
            hvn,
        )

    def _step_fsms(self, h1: Bar, prev_close: float, res: CloseResult) -> None:
        s = self.series[TF.H1]
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
            if z.lo - PROXIMITY_ATR * atr <= h1.close <= z.hi + PROXIMITY_ATR * atr
        }
        for zid in near:
            self.fsm.setdefault(zid, RetestFSM(zid))
        for zid in sorted(self.fsm):
            fsm = self.fsm[zid]
            zone = self.zones.get(zid)
            if zid not in near and fsm.state == "IDLE":
                del self.fsm[zid]
                continue
            armed = fsm.step(h1, zone, ctx, self.cfg.entries)
            if armed is None:
                continue
            out = build_signal(
                self.symbol,
                armed,
                zones=self.zones.zones,
                trend=self.trend,
                choch_side=self.structure[TF.H4].choch_side,
                legs_h4=self.legs_h4,
                atr_h1=atr,
                atr_h4=self.atr(TF.H4),
                vol_history=self.vol_history,
                zone_strength=zone.strength if zone else fsm.strength,
                zone_origin=zone.tf_origin if zone else set(),
                cfg=self.cfg,
            )
            (res.signals if isinstance(out, Signal) else res.rejected).append(out)

    # -- step 7 ------------------------------------------------------------------
    def manage(self, pos: Position) -> list[exits.MoveStop | exits.ExitNextOpen]:
        """Exit decisions for an open position at the current 1H close."""
        exits.observe(pos, self.series[TF.H1].last)
        return exits.evaluate(
            pos,
            self.atr(TF.H1),
            decay=self.decay(pos.side),
            choch_against=self.structure[TF.H4].choch_against(pos.side, pos.opened_at),
            fee_taker=self.cfg.exchange.fee_taker,
            cfg=self.cfg.exits,
        )
