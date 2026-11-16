"""Per-symbol signal engine: M1 in, entry intents out.

One `Market` per symbol. Feed it closed M1 bid bars in research-clock order with `on_m1`. On each call it

1. closes every higher-timeframe bucket that ended at or before this minute (day, 4H, 1H first, then 5/10/15M),
   updating indicators, the HTF trend, the 1H rule context and the pattern detectors;
2. turns the signals of the closed LTF bars into entries at this minute (the first M1 at or after the signal bar
   close, as in the research), applying entry gating, signal qualification, trend filters, confluence and the
   one-position rule through the `is_flat` callback;
3. adds the M1 bar to the open buckets.

The same object runs in replay (npz history) and live (broker M1), so the live signal set is the tested one.
Research references: ac_run.py (pattern strategies, S20), hs.py (S07), hs5.py (S08, S12), lib.build/top20.py (rules).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from .bars import Aggregator, Bar, day_aggregator
from .clock import MAX_ENTRY_DELAY, in_no_entry, trading_day
from .config import Strategy
from .indicators import NAN, Adx, Atr, Ewm, Lagged, Rsi
from .patterns import HsDetector, PatternDetector


@dataclass
class Signal:
    """A detector/rule signal waiting for its entry minute."""

    sid_hint: str  # 'patterns' | 'hs' | 'rule'
    tf: int
    k: int | None
    pattern: str
    side: int
    bar_start: int
    close_t: int
    atr: float
    stop: float  # pattern stop (qualification only); NaN for rules
    trend: dict[int, int]  # HTF minutes -> trend at the signal bar
    detail: dict = field(default_factory=dict)
    rule: str | None = None


@dataclass
class Entry:
    """An entry the strategy takes now (market at the open of minute t)."""

    strategy: Strategy
    side: int
    t: int
    price: float  # open of minute t (replay) / reference price (live, filled by the caller)
    signal: Signal


@dataclass
class Skip:
    strategy: Strategy
    t: int
    reason: str
    signal: Signal


class Trend:
    """lib/ac_run HTF trend: close vs EMA50 and EMA50 vs five bars earlier, on each closed HTF bar."""

    def __init__(self):
        self.ema = Ewm.span(50)
        self.lag = Lagged(5)
        self.by_start: dict[int, int] = {}
        self.order: deque[int] = deque()

    def on_bar(self, b: Bar) -> None:
        e = self.ema.update(b.c)
        self.lag.push(e)
        e5 = self.lag.ago(5)
        up = b.c > e and e > e5
        dn = b.c < e and e < e5
        self.by_start[b.start] = int(up) - int(dn)
        self.order.append(b.start)
        if len(self.order) > 5000:
            del self.by_start[self.order.popleft()]

    def at_close(self, ltf_end: int, minutes: int) -> int:
        """Trend of the last closed HTF bar at an LTF close: bar starting floor(end, HTF) - HTF (0 if missing)."""
        key = ltf_end // minutes * minutes - minutes
        return self.by_start.get(key, 0)


class RuleContext:
    """The 1H and trading-day conditions of S10/S18/S19 (lib.htf_conditions), keyed by 1H bar start."""

    def __init__(self):
        self.atr14, self.atr100, self.adx = Atr(14), Atr(100), Adx(14)
        self.days: deque[tuple[int, float, float]] = deque(maxlen=3)  # (trading day, high, low) of closed days
        self.by_start: dict[int, tuple[bool, bool, bool]] = {}
        self.order: deque[int] = deque()

    def on_day(self, b: Bar) -> None:
        self.days.append((trading_day(b.start), b.h, b.l))

    def on_hour(self, b: Bar) -> None:
        a14 = self.atr14.update(b.h, b.l, b.c)
        a100 = self.atr100.update(b.h, b.l, b.c)
        ad = self.adx.update(b.h, b.l, b.c)
        va = a14 / a100 if a100 else NAN
        vol_hi = va > 1.25
        adx_lt20 = ad < 20
        td = trading_day(b.start)
        prior = [d for d in self.days if d[0] < td]
        if prior:
            _, ph, pl = prior[-1]
            inside = pl <= b.c <= ph
        else:
            inside = False
        self.by_start[b.start] = (vol_hi, adx_lt20, inside)
        self.order.append(b.start)
        if len(self.order) > 5000:
            del self.by_start[self.order.popleft()]

    def at_close(self, ltf_end: int) -> tuple[bool, bool, bool]:
        key = ltf_end // 60 * 60 - 60
        return self.by_start.get(key, (False, False, False))


class Rule15:
    """15M trigger conditions for the rules (lib.ltf_conditions subset) + first-bar-true memory."""

    def __init__(self, rules: list[str]):
        self.rules = rules
        self.rsi2 = Rsi(2)
        self.e20, self.e50 = Ewm.span(20), Ewm.span(50)
        self.prev_e20 = NAN
        self.prev_e50 = NAN
        self.prev_o = NAN
        self.prev_c = NAN
        self.prev_m = {r: False for r in rules}

    def on_bar(self, b: Bar, a: float, ctx: tuple[bool, bool, bool]) -> list[str]:
        r2 = self.rsi2.update(b.c)
        e20, e50 = self.e20.update(b.c), self.e50.update(b.c)
        ext_up = (b.c - e50) > 2 * a
        ext_dn = (b.c - e50) < -2 * a
        xup = e20 > e50 and self.prev_e20 <= self.prev_e50
        engulf = b.c > b.o and self.prev_c < self.prev_o and b.c >= self.prev_o and b.o <= self.prev_c
        vol_hi, adx_lt20, inside = ctx
        m = {
            "rule_C1": vol_hi and r2 < 10 and ext_dn,
            "rule_C2": adx_lt20 and engulf and ext_up,
            "rule_C3": inside and xup and ext_up,
        }
        fired = [r for r in self.rules if m[r] and not self.prev_m[r]]
        for r in self.rules:
            self.prev_m[r] = m[r]
        self.prev_e20, self.prev_e50, self.prev_o, self.prev_c = e20, e50, b.o, b.c
        return fired


RULE_SIDE = {"rule_C1": -1, "rule_C2": 1, "rule_C3": -1}


class Market:
    def __init__(self, symbol: str, strategies: list[Strategy], cost: float):
        self.symbol = symbol
        self.strategies = [s for s in strategies if s.symbol == symbol]
        self.cost = cost
        ltfs = sorted({tf for s in self.strategies for tf in s.timeframes})
        self.ltf_aggs = {tf: Aggregator(tf) for tf in ltfs}
        self.ltf_atr = {tf: Atr(14) for tf in ltfs}
        self.h1, self.h4, self.day = Aggregator(60), Aggregator(240), day_aggregator()
        self.trend = {60: Trend(), 240: Trend()}
        self.pat: dict[tuple[int, int], PatternDetector] = {}
        self.hs: dict[tuple[int, int], HsDetector] = {}
        rules = []
        for s in self.strategies:
            for tf in s.timeframes:
                if s.engine in ("patterns", "patterns_confluence"):
                    self.pat.setdefault((tf, s.pivot_k), PatternDetector(s.pivot_k))
                elif s.engine in ("hs", "hs_confluence"):
                    self.hs.setdefault((tf, s.pivot_k), HsDetector(s.pivot_k))
            if s.engine.startswith("rule_"):
                rules.append(s.engine)
        self.rule_ctx = RuleContext() if rules else None
        self.rule15 = Rule15(rules) if rules else None
        # confluence memory: strategy id -> deque of (entry t, side, source)
        self.confl: dict[str, deque] = {s.id: deque() for s in self.strategies if s.confluence_window}
        self.pending: list[Signal] = []
        self.n_m1 = 0
        self.last_t: int | None = None

    # ------------------------------------------------------------------ bars
    def _close_buckets(self, t: int) -> list[tuple[int, Bar]]:
        d = self.day.flush_until(t)
        if d is not None and self.rule_ctx:
            self.rule_ctx.on_day(d)
        b4 = self.h4.flush_until(t)
        if b4 is not None:
            self.trend[240].on_bar(b4)
        b1 = self.h1.flush_until(t)
        if b1 is not None:
            self.trend[60].on_bar(b1)
            if self.rule_ctx:
                self.rule_ctx.on_hour(b1)
        closed = []
        for tf, agg in self.ltf_aggs.items():
            b = agg.flush_until(t)
            if b is not None:
                closed.append((tf, b))
        return closed

    def _on_ltf(self, tf: int, b: Bar) -> None:
        a = self.ltf_atr[tf].update(b.h, b.l, b.c)
        trend = {60: self.trend[60].at_close(b.end, 60), 240: self.trend[240].at_close(b.end, 240)}
        for (ptf, k), det in self.pat.items():
            if ptf != tf:
                continue
            for s in det.step(b.h, b.l, b.c, a):
                self.pending.append(Signal("patterns", tf, k, s["pattern"], int(s["side"]), b.start, b.end, s["atr"],
                                           s["stop"], trend, detail=s))
        for (ptf, k), det in self.hs.items():
            if ptf != tf:
                continue
            for s in det.step(b.h, b.l, b.c, a):
                side = int(s["side"])
                stop = s["H3"] + 0.2 * s["atr"] if side == -1 else s["H3"] - 0.2 * s["atr"]
                self.pending.append(Signal("hs", tf, k, "head_shoulders" if side == -1 else "inverse_hs", side,
                                           b.start, b.end, s["atr"], stop, trend, detail=s))
        if tf == 15 and self.rule15 is not None:
            ctx = self.rule_ctx.at_close(b.end)
            for r in self.rule15.on_bar(b, a, ctx):
                self.pending.append(Signal("rule", tf, None, r, RULE_SIDE[r], b.start, b.end, a, NAN, trend, rule=r))

    # --------------------------------------------------------------- signals
    def _qualify(self, s: Strategy, sig: Signal, t: int, e: float) -> str | None:
        """None if the signal is valid for strategy s with entry at minute t / price e, else the skip reason."""
        if sig.sid_hint == "rule":
            if sig.rule != s.engine:
                return "n/a"
        elif sig.sid_hint == "patterns":
            if s.engine not in ("patterns", "patterns_confluence") or sig.k != s.pivot_k or sig.tf not in s.timeframes:
                return "n/a"
            if sig.pattern not in s.patterns:
                return "n/a"
        else:
            if s.engine not in ("hs", "hs_confluence") or sig.k != s.pivot_k or sig.tf not in s.timeframes:
                return "n/a"
            if s.side_filter is not None and sig.side != s.side_filter:
                return "n/a"
        if s.trend_filter is not None and sig.trend[s.trend_filter] != sig.side:
            return "trend"
        # entry gating (research: next M1 at or after the close, <= 30 min, outside 16:00-18:59)
        if in_no_entry(t):
            return "no_entry_window"
        if t - sig.close_t > MAX_ENTRY_DELAY:
            return "entry_delay"
        if sig.sid_hint == "rule":
            if trading_day(sig.bar_start) != trading_day(t):
                return "entry_other_day"
            return None
        if sig.sid_hint == "patterns" or (sig.sid_hint == "hs" and s.qualification != "risk_atr_only"):
            if not sig.atr > 0:
                return "atr_zero"
        risk = sig.side * (e - sig.stop)
        if not (0.5 * sig.atr <= risk <= 4 * sig.atr):
            return "risk_atr"
        if s.qualification == "risk_atr_and_3x_cost" and not risk >= 3 * self.cost:
            return "risk_cost"
        return None

    def _source(self, s: Strategy, sig: Signal) -> str:
        return f"{sig.pattern}@{sig.tf}" if s.engine == "patterns_confluence" else str(sig.tf)

    def _resolve(self, t: int, e: float, is_flat: Callable[[str], bool]) -> tuple[list[Entry], list[Skip]]:
        entries: list[Entry] = []
        skips: list[Skip] = []
        sigs, self.pending = self.pending, []
        taken: set[str] = set()
        for s in self.strategies:
            for sig in sigs:
                why = self._qualify(s, sig, t, e)
                if why == "n/a":
                    continue
                if why is not None:
                    skips.append(Skip(s, t, why, sig))
                    continue
                if s.confluence_window:
                    q = self.confl[s.id]
                    while q and q[0][0] < t - s.confluence_window:
                        q.popleft()
                    src = self._source(s, sig)
                    ok = any(sd == sig.side and so != src for _, sd, so in q)
                    q.append((t, sig.side, src))
                    if not ok:
                        skips.append(Skip(s, t, "no_confluence", sig))
                        continue
                if s.id in taken or not is_flat(s.id):
                    skips.append(Skip(s, t, "position_open", sig))
                    continue
                if not s.enabled:
                    skips.append(Skip(s, t, "disabled", sig))
                    continue
                taken.add(s.id)
                entries.append(Entry(s, sig.side, t, e, sig))
        return entries, skips

    # ------------------------------------------------------------------- feed
    def on_m1(self, t: int, o: float, h: float, l: float, c: float,
              is_flat: Callable[[str], bool]) -> tuple[list[Entry], list[Skip]]:
        if self.last_t is not None and t <= self.last_t:
            return [], []
        self.last_t = t
        for tf, b in self._close_buckets(t):
            self._on_ltf(tf, b)
        entries, skips = self._resolve(t, o, is_flat) if self.pending else ([], [])
        self.day.add(t, o, h, l, c)
        self.h4.add(t, o, h, l, c)
        self.h1.add(t, o, h, l, c)
        for agg in self.ltf_aggs.values():
            agg.add(t, o, h, l, c)
        self.n_m1 += 1
        return entries, skips

    def flush(self, t: int) -> None:
        """Live: close buckets that ended by research minute t without waiting for the next M1 (signals stay pending
        until the next `on_m1`, whose minute is the entry minute)."""
        for tf, b in self._close_buckets(t):
            self._on_ltf(tf, b)
