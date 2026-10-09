"""One accept/reject decision for a signal, shared by the backtester and live.

The engine and the live runner build a ``DecisionContext`` from their own
state (simulated or real) and call ``decide``; everything after that point is
the same code, which is what makes a live signal's fate match the backtest's.

Order of checks: universe membership, quarantine, data sanity, the
liquidation rule, sizing (minimum notional), then the portfolio guard.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from zone_trend.config import Config
from zone_trend.risk.liquidation import DEFAULT_BRACKET, Bracket, stop_within_liq
from zone_trend.risk.portfolio import OpenExposure, PortfolioGuard
from zone_trend.risk.sizing import Filters, Size, size_order
from zone_trend.types import Signal, Trend

__all__ = ["Decision", "DecisionContext", "decide", "slippage"]

TOP_SLIPPAGE = 0.0002
OTHER_SLIPPAGE = 0.0005
TOP_N = 5


def slippage(rank: int, tick: float, price: float, mult: float = 1.0) -> float:
    """Model slippage as a price fraction: 1 tick + 0.02% (top 5) or 0.05%."""
    base = TOP_SLIPPAGE if rank < TOP_N else OTHER_SLIPPAGE
    return (base + tick / price) * mult


@dataclass
class DecisionContext:
    t: int
    equity: float
    available: float
    rank: int | None  # position in this month's universe; None when not a member
    quarantined: bool
    data_flagged: bool
    exposure: list[OpenExposure]
    corr: Callable[[str], float]  # correlation of the signal's symbol with another
    funding_8h: float | None
    btc_trend: Trend | None
    filters: Filters
    k_sym: float = 1.0
    tp1_fraction: float = 0.4
    slippage_mult: float = 1.0
    bracket: Bracket = field(default=DEFAULT_BRACKET)
    stage_mult: float = 1.0  # live rollout stage scaling (0.5 at "half")


@dataclass(frozen=True)
class Decision:
    accepted: bool
    stage: str
    reason: str = ""
    size: Size | None = None
    slip: float = 0.0
    size_mult: float = 1.0


def decide(sig: Signal, ctx: DecisionContext, cfg: Config, guard: PortfolioGuard) -> Decision:
    if ctx.rank is None:
        return Decision(False, "universe", "not_in_universe")
    if ctx.quarantined:
        return Decision(False, "data", "quarantine")
    if ctx.data_flagged:
        return Decision(False, "data", "data_sanity")
    lev = cfg.exchange.leverage
    if not stop_within_liq(sig.entry_ref, sig.stop, sig.side.sign, lev, ctx.bracket):
        return Decision(False, "risk", "liquidation_rule")
    mult = guard.size_mult() * ctx.stage_mult
    size = size_order(
        sig.entry_ref,
        sig.stop,
        ctx.equity,
        ctx.available,
        cfg.sizing,
        cfg.exchange,
        ctx.filters,
        ctx.tp1_fraction,
        ctx.k_sym,
        mult,
    )
    if size is None:
        return Decision(False, "risk", "min_notional")
    corrs = {e.symbol: ctx.corr(e.symbol) for e in ctx.exposure}
    reason = guard.check(
        sig, ctx.t, ctx.equity, size.notional, ctx.exposure, corrs, ctx.funding_8h, ctx.btc_trend
    )
    if reason:
        return Decision(False, "risk", reason)
    slip = slippage(ctx.rank, ctx.filters.tick, sig.entry_ref, ctx.slippage_mult)
    return Decision(True, "risk", "", size, slip, mult)
