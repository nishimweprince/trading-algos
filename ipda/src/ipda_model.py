"""ICT IPDA entry model: data range -> liquidity sweep in a killzone -> structure shift ->
limit at the displacement FVG. Rules: docs/ipda-model.md (v1).

The model is a streaming state machine fed one closed 1-minute candle at a time, so the live
service and the backtest run the very same code. It aggregates its own target-timeframe bars,
evaluates structure only on *closed* bars (no forming-bar repaint), and keeps its own daily
history, which the live service seeds from D1 candles at startup.

Definitions reused from file.txt:
* Section 1 fractal (``ms_length``): ``dh = sum(sign(high - high[1]), p)``,
  ``bullf = dh == -p and dh[p] == p and high[p] == highest(ms_length)`` (mirrored for lows),
  and a break is ``crossover(close, fractal)`` on a fractal not yet crossed.
* Section 2 FVG: bullish ``low > high[2] and close[1] > high[2]``, bearish mirrored.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .candles import Candle

NY = ZoneInfo("America/New_York")
_ROLL = timedelta(hours=7)  # 17:00 New York + 7h = midnight -> next trading day's date


def trading_day(moment: datetime) -> date:
    """Trading-day key: the 17:00 New York roll belongs to the next calendar date."""
    return (moment.astimezone(NY) + _ROLL).date()


@dataclass(frozen=True, slots=True)
class Killzone:
    name: str
    start: time
    end: time


DEFAULT_KILLZONES: tuple[Killzone, ...] = (
    Killzone("london", time(2, 0), time(5, 0)),
    Killzone("ny_am", time(7, 0), time(10, 0)),
)


@dataclass(slots=True)
class IpdaParams:
    tf_minutes: int = 3
    fractal_len: int = 5
    range_days: int = 20
    pd_filter: bool = True
    killzones: tuple[Killzone, ...] = DEFAULT_KILLZONES
    asia_start: time = time(20, 0)  # to midnight, at the start of the trading day
    risk_reward: float = 2.0
    target_mode: str = "rr"  # "rr" | "liquidity"
    stop_buffer: float = 0.0  # price units
    min_risk: float = 1.0  # price units
    max_risk: float = 15.0  # price units


@dataclass(frozen=True, slots=True)
class Setup:
    direction: str  # "buy" | "sell"
    entry: float
    stop_loss: float
    take_profit: float
    created_at: datetime  # close of the 3M bar that confirmed the shift (UTC)
    bucket_start: datetime  # that bar's start; keys the signal id
    expires_at: datetime  # killzone end (UTC)
    killzone: str
    sweep_extreme: float
    fvg_top: float
    fvg_bottom: float
    equilibrium: float | None


@dataclass(slots=True)
class _Bar:
    start: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(slots=True)
class _Fractal:
    value: float | None = None
    crossed: bool = True


@dataclass(slots=True)
class _Fvg:
    bar_start: datetime
    top: float
    bottom: float
    bullish: bool


@dataclass(slots=True)
class _KillzoneState:
    key: tuple[date, str]
    zone: Killzone
    expires_at: datetime
    used: bool = False
    sell_sweep: float | None = None  # lowest low since a sell-side sweep
    sell_sweep_bucket: datetime | None = None
    buy_sweep: float | None = None
    buy_sweep_bucket: datetime | None = None


@dataclass(slots=True)
class _Day:
    key: date | None = None
    high: float = float("-inf")
    low: float = float("inf")
    counts: bool = True  # False while replaying minutes of a day already seeded from D1
    asia_high: float | None = None
    asia_low: float | None = None


class IpdaModel:
    def __init__(self, params: IpdaParams | None = None) -> None:
        self.p = params or IpdaParams()
        self.stats: Counter[str] = Counter()
        self._days: deque[tuple[date, float, float]] = deque(maxlen=max(self.p.range_days, 60))
        self._seeded_through: date | None = None
        self._day = _Day()
        self._bars: deque[_Bar] = deque(maxlen=max(16, self.p.fractal_len * 2 + 2))
        self._forming: _Bar | None = None
        self._upper = _Fractal()
        self._lower = _Fractal()
        self._fvgs: deque[_Fvg] = deque(maxlen=64)
        self._kz: _KillzoneState | None = None
        self.last_minute: datetime | None = None

    # ------------------------------------------------------------------ inputs
    def seed_daily(self, days: list[tuple[date, float, float]]) -> None:
        """Completed trading days (key, high, low), oldest first, e.g. from D1 candles."""
        for key, high, low in sorted(days):
            if self._days and key <= self._days[-1][0]:
                continue
            self._days.append((key, high, low))
        if self._days:
            self._seeded_through = self._days[-1][0]

    def on_minute(self, c: Candle) -> Setup | None:
        """Feed one closed 1M candle (start-stamped, tz-aware). Returns a new setup or None."""
        if self.last_minute is not None and c.start <= self.last_minute:
            return None
        self.last_minute = c.start
        tf = self.p.tf_minutes
        bucket = _bucket_start(c.start, tf)
        setup: Setup | None = None

        if self._forming is not None and bucket != self._forming.start:
            setup = self._close_bar()  # the previous bucket never saw its last minute

        local = c.start.astimezone(NY)
        self._update_day(c, local)
        self._update_killzone(c, local, bucket)

        if self._forming is None:
            self._forming = _Bar(bucket, c.open, c.high, c.low, c.close)
        else:
            f = self._forming
            f.high = max(f.high, c.high)
            f.low = min(f.low, c.low)
            f.close = c.close
        if c.start == bucket + timedelta(minutes=tf - 1):
            setup = self._close_bar() or setup
        return setup

    # ------------------------------------------------------------------ daily state
    def _update_day(self, c: Candle, local: datetime) -> None:
        key = (local + _ROLL).date()
        d = self._day
        if key != d.key:
            if d.key is not None and d.counts and d.high > float("-inf"):
                if not self._days or d.key > self._days[-1][0]:
                    self._days.append((d.key, d.high, d.low))
            counts = self._seeded_through is None or key > self._seeded_through
            self._day = d = _Day(key=key, counts=counts)
        d.high = max(d.high, c.high)
        d.low = min(d.low, c.low)
        if local.time() >= self.p.asia_start:
            d.asia_high = c.high if d.asia_high is None else max(d.asia_high, c.high)
            d.asia_low = c.low if d.asia_low is None else min(d.asia_low, c.low)

    def _previous_day(self) -> tuple[float, float] | None:
        """PDH/PDL: the last completed day before the current trading day."""
        for key, high, low in reversed(self._days):
            if self._day.key is None or key < self._day.key:
                return high, low
        return None

    def equilibrium(self) -> float | None:
        days = [d for d in self._days if self._day.key is None or d[0] < self._day.key]
        if len(days) < self.p.range_days:
            return None
        window = days[-self.p.range_days :]
        return (max(d[1] for d in window) + min(d[2] for d in window)) / 2

    # ------------------------------------------------------------------ killzones
    def _update_killzone(self, c: Candle, local: datetime, bucket: datetime) -> None:
        zone = _zone_at(local.time(), self.p.killzones)
        if zone is None:
            self._kz = None
            return
        key = (self._day.key, zone.name)
        if self._kz is None or self._kz.key != key:
            end_local = datetime.combine(local.date(), zone.end, tzinfo=NY)
            self._kz = _KillzoneState(key=key, zone=zone, expires_at=end_local.astimezone(UTC))
        kz = self._kz
        if kz.used:
            return
        pdx = self._previous_day()
        lows = [v for v in (self._day.asia_low, pdx[1] if pdx else None) if v is not None]
        highs = [v for v in (self._day.asia_high, pdx[0] if pdx else None) if v is not None]
        if kz.sell_sweep is not None:
            kz.sell_sweep = min(kz.sell_sweep, c.low)
        elif lows and c.low < min(lows):
            kz.sell_sweep, kz.sell_sweep_bucket = c.low, bucket
            self.stats["sweep_sell_side"] += 1
        if kz.buy_sweep is not None:
            kz.buy_sweep = max(kz.buy_sweep, c.high)
        elif highs and c.high > max(highs):
            kz.buy_sweep, kz.buy_sweep_bucket = c.high, bucket
            self.stats["sweep_buy_side"] += 1

    # ------------------------------------------------------------------ 3M structure
    def _close_bar(self) -> Setup | None:
        bar = self._forming
        self._forming = None
        if bar is None:
            return None
        self._bars.append(bar)
        bull_break, bear_break = self._structure()
        self._record_fvg()
        kz = self._kz
        if kz is None or kz.used or _zone_at(bar.start.astimezone(NY).time(), (kz.zone,)) is None:
            return None
        setup = None
        if bull_break and kz.sell_sweep is not None:
            self.stats["mss_bull"] += 1
            setup = self._build(kz, bar, "buy")
        if setup is None and bear_break and kz.buy_sweep is not None:
            self.stats["mss_bear"] += 1
            setup = self._build(kz, bar, "sell")
        if setup is not None:
            kz.used = True
            self.stats["setup"] += 1
        return setup

    def _structure(self) -> tuple[bool, bool]:
        b = self._bars
        n = len(b) - 1
        length = self.p.fractal_len
        p = length // 2
        if n >= 2 * p + 1 and n >= length - 1:

            def dsum(attr: str, end: int) -> int:
                return sum(
                    _sign(getattr(b[i], attr) - getattr(b[i - 1], attr))
                    for i in range(end - p + 1, end + 1)
                )

            window = [b[i] for i in range(n - length + 1, n + 1)]
            if (
                dsum("high", n) == -p
                and dsum("high", n - p) == p
                and b[n - p].high == max(x.high for x in window)
            ):
                self._upper = _Fractal(b[n - p].high, False)
            if (
                dsum("low", n) == p
                and dsum("low", n - p) == -p
                and b[n - p].low == min(x.low for x in window)
            ):
                self._lower = _Fractal(b[n - p].low, False)
        bull = bear = False
        if n >= 1:
            close, prev = b[n].close, b[n - 1].close
            up, lo = self._upper, self._lower
            if not up.crossed and up.value is not None and close > up.value >= prev:
                up.crossed = bull = True
            if not lo.crossed and lo.value is not None and close < lo.value <= prev:
                lo.crossed = bear = True
        return bull, bear

    def _record_fvg(self) -> None:
        b = self._bars
        if len(b) < 3:
            return
        cur, mid, old = b[-1], b[-2], b[-3]
        if cur.low > old.high and mid.close > old.high:
            self._fvgs.append(_Fvg(cur.start, cur.low, old.high, True))
        elif cur.high < old.low and mid.close < old.low:
            self._fvgs.append(_Fvg(cur.start, old.low, cur.high, False))

    def _build(self, kz: _KillzoneState, bar: _Bar, direction: str) -> Setup | None:
        bullish = direction == "buy"
        since = kz.sell_sweep_bucket if bullish else kz.buy_sweep_bucket
        fvg = next(
            (
                f
                for f in reversed(self._fvgs)
                if f.bullish == bullish and since is not None and f.bar_start >= since
            ),
            None,
        )
        if fvg is None:
            self.stats["reject_no_fvg"] += 1
            return None
        entry = (fvg.top + fvg.bottom) / 2
        eq = self.equilibrium()
        if self.p.pd_filter:
            if eq is None:
                self.stats["reject_no_range"] += 1
                return None
            if (bullish and entry >= eq) or (not bullish and entry <= eq):
                self.stats["reject_pd"] += 1
                return None
        if (bullish and bar.close <= entry) or (not bullish and bar.close >= entry):
            self.stats["reject_price_in_fvg"] += 1
            return None
        extreme = kz.sell_sweep if bullish else kz.buy_sweep
        assert extreme is not None
        stop = extreme - self.p.stop_buffer if bullish else extreme + self.p.stop_buffer
        risk = abs(entry - stop)
        if not (self.p.min_risk <= risk <= self.p.max_risk):
            self.stats["reject_risk"] += 1
            return None
        target = self._target(entry, risk, bullish)
        if target is None:
            self.stats["reject_target"] += 1
            return None
        end = bar.start + timedelta(minutes=self.p.tf_minutes)
        return Setup(
            direction=direction,
            entry=entry,
            stop_loss=stop,
            take_profit=target,
            created_at=end,
            bucket_start=bar.start,
            expires_at=kz.expires_at,
            killzone=kz.zone.name,
            sweep_extreme=extreme,
            fvg_top=fvg.top,
            fvg_bottom=fvg.bottom,
            equilibrium=eq,
        )

    def _target(self, entry: float, risk: float, bullish: bool) -> float | None:
        sign = 1 if bullish else -1
        if self.p.target_mode == "rr":
            return entry + sign * self.p.risk_reward * risk
        pdx = self._previous_day()
        pools = (
            [self._day.asia_high, pdx[0] if pdx else None]
            if bullish
            else [self._day.asia_low, pdx[1] if pdx else None]
        )
        ahead = [v for v in pools if v is not None and sign * (v - entry) >= risk]
        if not ahead:
            return None
        return min(ahead) if bullish else max(ahead)


def _sign(x: float) -> int:
    return int(x > 0) - int(x < 0)


def _bucket_start(ts: datetime, tf: int) -> datetime:
    utc = ts.astimezone(UTC)
    midnight = utc.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes = int((utc - midnight).total_seconds() // 60)
    return midnight + timedelta(minutes=(minutes // tf) * tf)


def _zone_at(t: time, zones: tuple[Killzone, ...]) -> Killzone | None:
    for z in zones:
        if z.start <= t < z.end:
            return z
    return None
