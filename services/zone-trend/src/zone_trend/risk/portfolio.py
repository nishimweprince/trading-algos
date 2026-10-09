"""Portfolio limits, entry filters and breakers.

Entry checks, in order (the first failure is the rejection reason):

==================  ===========================================================
halted              drawdown from peak reached ``dd_halt_pct`` (manual restart)
paused              daily or weekly loss breaker is cooling down
symbol_open         already a position on the symbol (one per symbol)
max_positions       ``max_positions`` open
max_notional        open notional + this one over ``max_total_notional_pct`` x equity
correlated          ``max_same_dir_correlated`` same-side positions already open on
                    symbols with 30-day daily-return correlation over the threshold
funding             long with funding above +``funding_max_8h`` per 8h, or a short
                    below the negative of it (last settled rate, 8h-normalised)
btc_gate            alt long while BTC 1D is Down, alt short while it is Up
==================  ===========================================================

Breakers, updated on each hourly mark:

- daily loss: equity at or below the 00:00 UTC anchor x (1 - daily_loss_pct)
  -> no entries until the next 00:00 UTC;
- weekly loss: at or below the Monday 00:00 UTC anchor x (1 - weekly_loss_pct)
  -> no entries for 72 hours;
- drawdown from peak at or over ``dd_halve_pct`` -> sizes halved while it lasts;
  at or over ``dd_halt_pct`` -> halted, which needs a manual restart;
- ``consec_losses_halve`` losing trades in a row -> sizes halved for the next
  10 trades.

The funding blackout (no adverse entries in the 15 minutes before a
settlement) never binds on hourly market entries, which fill at the open,
on or after the settlement; it matters only for limit entries, which are not
built.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from zone_trend.config import Filters, Portfolio
from zone_trend.types import Side, Signal, Trend

__all__ = ["OpenExposure", "PortfolioGuard"]

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
HALVE_TRADES = 10
BTC = "BTCUSDT"


@dataclass(frozen=True)
class OpenExposure:
    symbol: str
    side: Side
    notional: float


@dataclass
class PortfolioGuard:
    limits: Portfolio
    filters: Filters
    peak: float = 0.0
    day_anchor: float = 0.0
    week_anchor: float = 0.0
    day: int = -1
    week: int = -1
    paused_until: int = 0
    halted: bool = False
    halved_dd: bool = False
    consec_losses: int = 0
    halve_trades_left: int = 0
    events: list[tuple[int, str]] = field(default_factory=list)

    # -- breakers ----------------------------------------------------------------
    def mark(self, t: int, equity: float) -> None:
        lim = self.limits
        day = (int(t) + 1) // DAY_MS  # t is a close time (...59.999)
        week = (day + 3) // 7  # weeks starting Monday 00:00 UTC (1970-01-01 was a Thursday)
        if day != self.day:
            self.day, self.day_anchor = day, equity
        if week != self.week:
            self.week, self.week_anchor = week, equity
        self.peak = max(self.peak, equity)
        if self.peak <= 0:
            return
        dd = 1 - equity / self.peak
        if dd >= lim.dd_halt_pct and not self.halted:
            self.halted = True
            self.events.append((t, "halt_drawdown"))
        halve = dd >= lim.dd_halve_pct
        if halve and not self.halved_dd:
            self.events.append((t, "halve_drawdown"))
        self.halved_dd = halve
        if self.paused_until <= t:
            if equity <= self.day_anchor * (1 - lim.daily_loss_pct):
                self.paused_until = (day + 1) * DAY_MS - 1
                self.events.append((t, "pause_daily_loss"))
            if equity <= self.week_anchor * (1 - lim.weekly_loss_pct):
                self.paused_until = max(self.paused_until, t + 72 * HOUR_MS)
                self.events.append((t, "pause_weekly_loss"))

    def trade_closed(self, t: int, net_pnl: float) -> None:
        self.consec_losses = self.consec_losses + 1 if net_pnl < 0 else 0
        if self.consec_losses >= self.limits.consec_losses_halve:
            self.halve_trades_left = HALVE_TRADES
            self.consec_losses = 0
            self.events.append((t, "halve_consecutive_losses"))

    def size_mult(self) -> float:
        return 0.5 if (self.halved_dd or self.halve_trades_left > 0) else 1.0

    def trade_opened(self) -> None:
        if self.halve_trades_left > 0:
            self.halve_trades_left -= 1

    # -- entry checks --------------------------------------------------------------
    def check(
        self,
        sig: Signal,
        t: int,
        equity: float,
        notional: float,
        open_pos: list[OpenExposure],
        corr: dict[str, float],
        funding_8h: float | None,
        btc_trend: Trend | None,
    ) -> str | None:
        lim, flt = self.limits, self.filters
        if self.halted:
            return "halted"
        if self.paused_until > t:
            return "paused"
        if any(p.symbol == sig.symbol for p in open_pos):
            return "symbol_open"
        if len(open_pos) >= lim.max_positions:
            return "max_positions"
        if sum(p.notional for p in open_pos) + notional > lim.max_total_notional_pct * equity:
            return "max_notional"
        same = [
            p
            for p in open_pos
            if p.side is sig.side and corr.get(p.symbol, 0.0) > flt.corr_threshold
        ]
        if len(same) >= lim.max_same_dir_correlated:
            return "correlated"
        if funding_8h is not None:
            if sig.side is Side.LONG and funding_8h > flt.funding_max_8h:
                return "funding"
            if sig.side is Side.SHORT and funding_8h < -flt.funding_max_8h:
                return "funding"
        if flt.btc_gate and sig.symbol != BTC and btc_trend is not None:
            if sig.side is Side.LONG and btc_trend == Trend.DOWN:
                return "btc_gate"
            if sig.side is Side.SHORT and btc_trend == Trend.UP:
                return "btc_gate"
        return None
