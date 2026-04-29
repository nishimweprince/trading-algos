"""SMC structure state machine — port of smc.txt lines 241-940 (TradingHub).

Stateful, bar-by-bar. Public output: current Bias and a stream of
StructureEvent (BOS/CHoCH/IDM/HH/HL/LH/LL).

Pine variable names are preserved in comments so this stays auditable
against the original indicator. The "Choch with IDM" vs "Choch without IDM"
behaviour is selected via the `structure_type` arg (matches Pine input).

Visual side-effects (line.new / label.new / sweepHL drawing) are dropped;
only their detection edges are emitted as events.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import pandas as pd

from app.core.types import (
    Bias, Direction, StructureEvent, StructureEventType,
)
from app.indicators.common import index_time


def _is_green(o: float, c: float) -> bool:
    return c > o


@dataclass
class SMCStructureState:
    # ─── Mother bar (inside-bar parent) ──────────────────────────────────
    motherHigh: float = float('nan')
    motherLow: float = float('nan')
    motherBar: Optional[datetime] = None

    # ─── Pullback extremes ───────────────────────────────────────────────
    puHigh: float = float('nan')
    puLow: float = float('nan')
    puHBar: Optional[datetime] = None
    puLBar: Optional[datetime] = None

    # ─── Current rolling swing ───────────────────────────────────────────
    H: float = float('-inf')
    L: float = float('inf')
    HBar: Optional[datetime] = None
    LBar: Optional[datetime] = None

    # ─── Last confirmed swing (drives BOS/CHoCH) ─────────────────────────
    lastH: float = float('-inf')
    lastL: float = float('inf')
    lastHBar: Optional[datetime] = None
    lastLBar: Optional[datetime] = None

    # ─── Secondary refs (IDM colouring in Pine) ──────────────────────────
    H_lastH: float = float('nan')
    L_lastHH: float = float('nan')
    H_lastLL: float = float('nan')
    L_lastL: float = float('nan')

    # ─── IDM candidates ──────────────────────────────────────────────────
    idmHigh: float = float('nan')
    idmLow: float = float('nan')
    idmHBar: Optional[datetime] = None
    idmLBar: Optional[datetime] = None

    # ─── Flags ───────────────────────────────────────────────────────────
    mnStrc: Optional[bool] = None
    prevMnStrc: Optional[bool] = None
    isPrevBos: Optional[bool] = None
    findIDM: bool = False
    isBosUp: bool = False
    isBosDn: bool = False
    isCocUp: bool = True
    isCocDn: bool = True
    initialised: bool = False

    # ─── History arrays (mirror Pine arr* state) ─────────────────────────
    arrTop: List[float] = field(default_factory=list)
    arrBot: List[float] = field(default_factory=list)
    arrTopBotBar: List[Optional[datetime]] = field(default_factory=list)

    arrIdmHigh: List[float] = field(default_factory=list)
    arrIdmLow: List[float] = field(default_factory=list)
    arrIdmHBar: List[Optional[datetime]] = field(default_factory=list)
    arrIdmLBar: List[Optional[datetime]] = field(default_factory=list)

    arrLastH: List[float] = field(default_factory=list)
    arrLastL: List[float] = field(default_factory=list)
    arrLastHBar: List[Optional[datetime]] = field(default_factory=list)
    arrLastLBar: List[Optional[datetime]] = field(default_factory=list)

    # ─── Bias derived from CHoCH state ───────────────────────────────────
    @property
    def bias(self) -> Bias:
        if self.isCocUp and not self.isCocDn:
            return Bias.BULLISH
        if self.isCocDn and not self.isCocUp:
            return Bias.BEARISH
        return Bias.NEUTRAL


def _last(arr: list, n: int = 1):
    if len(arr) >= n:
        return arr[-n]
    return None


def _push_topbot(s: SMCStructureState, h: float, l: float, t: datetime) -> None:
    s.arrTop.append(h)
    s.arrBot.append(l)
    s.arrTopBotBar.append(t)


def _push_idm_high(s: SMCStructureState) -> None:
    s.arrIdmHigh.append(s.puHigh)
    s.arrIdmHBar.append(s.puHBar)


def _push_idm_low(s: SMCStructureState) -> None:
    s.arrIdmLow.append(s.puLow)
    s.arrIdmLBar.append(s.puLBar)


def _push_lasthl(s: SMCStructureState) -> None:
    s.arrLastH.append(s.lastH)
    s.arrLastHBar.append(s.lastHBar)
    s.arrLastL.append(s.lastL)
    s.arrLastLBar.append(s.lastLBar)


class SMCStructure:
    """Bar-by-bar structure detector. Call step(df, i) per closed candle."""

    def __init__(self, structure_type: str = 'Choch without IDM'):
        if structure_type not in ('Choch without IDM', 'Choch with IDM'):
            raise ValueError(f'Unknown structure_type: {structure_type!r}')
        self.structure_type = structure_type
        self.state = SMCStructureState()

    # ──────────────────────────────────────────────────────────────────
    def step(self, df: pd.DataFrame, i: int) -> List[StructureEvent]:
        """Process bar i (0-indexed). Returns events fired on this bar."""
        s = self.state
        events: List[StructureEvent] = []
        if i < 1:
            self._init_bar(df, i)
            return events

        ts_py = index_time(df, i)
        o = float(df['open'].iloc[i]); h = float(df['high'].iloc[i])
        l = float(df['low'].iloc[i]); c = float(df['close'].iloc[i])
        po = float(df['open'].iloc[i - 1]); pc = float(df['close'].iloc[i - 1])

        # ── 1. Mother bar (inside-bar update) ────────────────────────
        is_inside = s.motherHigh > h and s.motherLow < l
        if not is_inside:
            s.motherHigh = h
            s.motherLow = l
            s.motherBar = ts_py

        # ── 2. Minor structure (3 branches) ──────────────────────────
        top = _last(s.arrTop, 1)
        bot = _last(s.arrBot, 1)
        topBotBar = _last(s.arrTopBotBar, 1)
        top1 = _last(s.arrTop, 2)
        bot1 = _last(s.arrBot, 2)
        topBotBar1 = _last(s.arrTopBotBar, 2)

        if top is not None and bot is not None:
            cur_green = _is_green(o, c)
            prev_green = _is_green(po, pc)

            if h >= top and l <= bot:                          # notrend
                if s.mnStrc is not None:
                    s.prevMnStrc = bool(s.mnStrc)
                else:
                    if s.prevMnStrc and cur_green and not prev_green:
                        s.puHigh = top
                        s.puHBar = topBotBar
                        if h > s.H:
                            _push_idm_low(s)
                    if (s.prevMnStrc is False) and (not cur_green) and prev_green:
                        s.puLow = bot
                        s.puLBar = topBotBar
                        if l < s.L:
                            _push_idm_high(s)
                if l < s.L and cur_green:
                    _push_idm_high(s)
                if h > s.H and not cur_green:
                    _push_idm_low(s)
                _push_topbot(s, h, l, ts_py)
                s.puHigh, s.puLow = h, l
                s.puHBar = ts_py
                s.puLBar = ts_py
                s.mnStrc = None

            elif h >= top and l > bot:                         # uptrend
                if s.prevMnStrc and s.mnStrc is None and top1 is not None:
                    s.puHigh = top1
                    s.puHBar = topBotBar1
                if h > s.H:
                    _push_idm_low(s)
                _push_topbot(s, h, l, ts_py)
                s.puHigh = h
                s.puHBar = ts_py
                s.prevMnStrc = None
                s.mnStrc = True

            elif h < top and l <= bot:                         # downtrend
                if (s.prevMnStrc is False) and s.mnStrc is None and bot1 is not None:
                    s.puLow = bot1
                    s.puLBar = topBotBar1
                if l < s.L:
                    _push_idm_high(s)
                _push_topbot(s, h, l, ts_py)
                s.puLow = l
                s.puLBar = ts_py
                s.prevMnStrc = None
                s.mnStrc = False

        # ── 3. Update rolling H / L + capture IDM candidate ─────────
        if h >= s.H:
            s.H = h
            s.HBar = ts_py
            s.L_lastHH = l
            il = _last(s.arrIdmLow, 1); ilb = _last(s.arrIdmLBar, 1)
            if il is not None:
                s.idmLow = il
                s.idmLBar = ilb
        if l <= s.L:
            s.L = l
            s.LBar = ts_py
            s.H_lastLL = h
            ih = _last(s.arrIdmHigh, 1); ihb = _last(s.arrIdmHBar, 1)
            if ih is not None:
                s.idmHigh = ih
                s.idmHBar = ihb

        # ── 4. Find IDM ─────────────────────────────────────────────
        if s.findIDM and s.isCocUp and s.isBosUp:
            if l < s.idmLow:
                if self.structure_type == 'Choch with IDM' and s.idmLow == s.lastL:
                    if s.isPrevBos:
                        self._fix_after_bos(Direction.BUY)
                    else:
                        self._fix_after_choch(Direction.BUY)
                s.findIDM = False
                s.isBosUp = False
                s.lastH = s.H
                s.lastHBar = s.HBar
                events.append(StructureEvent(ts_py, StructureEventType.IDM,
                                             Direction.BUY, s.idmLow))
                events.append(StructureEvent(ts_py, StructureEventType.HH,
                                             Direction.BUY, s.lastH))
                _push_lasthl(s)
                lh = _last(s.arrLastH, 1)
                if lh is not None:
                    s.H_lastH = lh
                s.L = l
                s.LBar = ts_py

        if s.findIDM and s.isCocDn and s.isBosDn:
            if h > s.idmHigh:
                if self.structure_type == 'Choch with IDM' and s.idmHigh == s.lastH:
                    if s.isPrevBos:
                        self._fix_after_bos(Direction.SELL)
                    else:
                        self._fix_after_choch(Direction.SELL)
                s.findIDM = False
                s.isBosDn = False
                s.lastL = s.L
                s.lastLBar = s.LBar
                events.append(StructureEvent(ts_py, StructureEventType.IDM,
                                             Direction.SELL, s.idmHigh))
                events.append(StructureEvent(ts_py, StructureEventType.LL,
                                             Direction.SELL, s.lastL))
                _push_lasthl(s)
                ll = _last(s.arrLastL, 1)
                if ll is not None:
                    s.L_lastL = ll
                s.H = h
                s.HBar = ts_py

        # ── 5. Check CHoCH ──────────────────────────────────────────
        if s.isCocDn and h > s.lastH:
            if c > s.lastH:
                events.append(StructureEvent(ts_py, StructureEventType.CHOCH,
                                             Direction.BUY, s.lastH))
                s.findIDM = True
                s.isBosUp = True
                s.isCocUp = True
                s.isBosDn = False
                s.isCocDn = False
                s.isPrevBos = False
                ll = _last(s.arrLastL, 1)
                if ll is not None:
                    s.L_lastL = ll

        if s.isCocUp and l < s.lastL:
            if c < s.lastL:
                events.append(StructureEvent(ts_py, StructureEventType.CHOCH,
                                             Direction.SELL, s.lastL))
                s.findIDM = True
                s.isBosUp = False
                s.isCocUp = False
                s.isBosDn = True
                s.isCocDn = True
                s.isPrevBos = False
                lh = _last(s.arrLastH, 1)
                if lh is not None:
                    s.H_lastH = lh

        # ── 6. Check BOS ────────────────────────────────────────────
        if (not s.findIDM) and (not s.isBosUp) and s.isCocUp:
            if h > s.lastH and c > s.lastH:
                events.append(StructureEvent(ts_py, StructureEventType.BOS,
                                             Direction.BUY, s.lastH))
                s.findIDM = True
                s.isBosUp = True
                s.isCocUp = True
                s.isBosDn = False
                s.isCocDn = False
                s.isPrevBos = True
                events.append(StructureEvent(ts_py, StructureEventType.HL,
                                             Direction.BUY, s.L))
                s.lastL = s.L
                s.lastLBar = s.LBar
                s.L_lastL = s.L

        if (not s.findIDM) and (not s.isBosDn) and s.isCocDn:
            if l < s.lastL and c < s.lastL:
                events.append(StructureEvent(ts_py, StructureEventType.BOS,
                                             Direction.SELL, s.lastL))
                s.findIDM = True
                s.isBosUp = False
                s.isCocUp = False
                s.isBosDn = True
                s.isCocDn = True
                s.isPrevBos = True
                events.append(StructureEvent(ts_py, StructureEventType.LH,
                                             Direction.SELL, s.H))
                s.lastH = s.H
                s.lastHBar = s.HBar
                s.H_lastH = s.H

        # ── 7. Trigger update last H/L ──────────────────────────────
        if h > s.lastH:
            s.lastH = h
            s.lastHBar = ts_py
        if l < s.lastL:
            s.lastL = l
            s.lastLBar = ts_py

        return events

    def _init_bar(self, df: pd.DataFrame, i: int) -> None:
        s = self.state
        if s.initialised:
            return
        ts_py = index_time(df, i)
        h = float(df['high'].iloc[i]); l = float(df['low'].iloc[i])
        s.motherHigh = h; s.motherLow = l; s.motherBar = ts_py
        s.puHigh = h; s.puLow = l; s.puHBar = ts_py; s.puLBar = ts_py
        s.H = h; s.L = l; s.HBar = ts_py; s.LBar = ts_py
        s.lastH = h; s.lastL = l; s.lastHBar = ts_py; s.lastLBar = ts_py
        s.idmHigh = h; s.idmLow = l; s.idmHBar = ts_py; s.idmLBar = ts_py
        _push_topbot(s, h, l, ts_py)
        s.initialised = True

    def _fix_after_bos(self, direction: Direction) -> None:
        """State-only equivalent of Pine fixStrcAfterBos().

        Pine deletes visual BOS/IDM/HL marks and restores the previous lastH/lastL
        reference when the IDM-gated CHoCH branch invalidates a just-confirmed BOS.
        Event logs are append-only, so this only corrects future state.
        """
        s = self.state
        if direction == Direction.BUY and len(s.arrLastL) >= 2:
            s.lastL = s.arrLastL[-2]
            s.lastLBar = s.arrLastLBar[-2]
        elif direction == Direction.SELL and len(s.arrLastH) >= 2:
            s.lastH = s.arrLastH[-2]
            s.lastHBar = s.arrLastHBar[-2]

    def _fix_after_choch(self, direction: Direction) -> None:
        """State-only equivalent of Pine fixStrcAfterChoch()."""
        s = self.state
        if direction == Direction.BUY and len(s.arrLastL) >= 3:
            s.lastL = s.arrLastL[-3]
            s.lastLBar = s.arrLastLBar[-3]
        elif direction == Direction.SELL and len(s.arrLastH) >= 3:
            s.lastH = s.arrLastH[-3]
            s.lastHBar = s.arrLastHBar[-3]

    def run(self, df: pd.DataFrame) -> List[StructureEvent]:
        events: List[StructureEvent] = []
        for i in range(len(df)):
            events.extend(self.step(df, i))
        return events
