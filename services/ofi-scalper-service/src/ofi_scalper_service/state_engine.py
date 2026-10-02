"""The feature engine: order-flow and book state -> one feature vector.

Deterministic, no I/O, no clock of its own. The live service and the research
code drive the same class (research imports this module; it never
re-implements a feature), so a feature can only mean one thing.

**The lookahead rule.** :meth:`MarketState.sample` at time ``t`` may only use
events stamped strictly before ``t``. Windowed sums enforce it by
construction (``[t - w, t)``); the book-derived features read the book as it
stands, so ``sample`` refuses (``LookaheadError``) when any event at or after
``t`` has already been fed. Drivers feed every event with ``recv_ns < t``,
then sample.

All timestamps are local receive times in nanoseconds (``recv_ns``).

Features (plan §1.3), per symbol:

- ``ofi_l{m}_{w}ms``: Cont's event OFI at book level m (1..10) summed over the
  window, in quote notional (price x qty), so BTC and ETH are comparable.
- ``ofi_int_{w}ms``: the level OFIs combined with ``pca_weights`` (fit on
  training data only; the default puts all weight on level 1).
- ``imbalance``: ``Qb / (Qb + Qa)`` at the touch.
- ``microprice_minus_mid_bp``: Stoikov-style. Without an adjustment table the
  microprice is the imbalance-weighted mid ``I*Pa + (1-I)*Pb``; with one, it is
  ``mid + g(imbalance bucket, spread ticks) * tick``.
- ``spread_ticks``, ``spread_bp``.
- ``tfi_{w}ms``: (aggressive buy - aggressive sell) / total volume.
- ``vwap_to_mid_bp_{w}ms``: trade VWAP over the window minus mid.
- ``rv_{w}s_bp``: std of grid mid log-returns over the window, per grid step.
- ``ret_{w}s_bp``: mid log-return over the window.
- ``depth_{bid|ask}_{b}bp``: quote notional within b bp of mid.
- ``xasset_ofi_l1_{w}ms``: the paired symbol's level-1 OFI, lagged.
- ``secs_to_funding``, ``funding_bucket``.

A feature without enough history is ``None``.

In ``BINANCE_FUTURES_BOOK_MODE=partial`` the book is a top-N snapshot every
100 ms: OFI is then measured between snapshots rather than per event, and the
depth bands sum only the levels the snapshot carries. Same code, coarser
input; never mix modes between training and live.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "DEPTH_BANDS_BP",
    "GRID_NS",
    "LEVELS",
    "OFI_WINDOWS_MS",
    "RV_WINDOWS_S",
    "TFI_WINDOWS_MS",
    "TREND_WINDOWS_S",
    "BookView",
    "EngineConfig",
    "GridClock",
    "LookaheadError",
    "MarketState",
    "PrefixSeries",
    "SymbolState",
    "feature_names",
    "funding_bucket",
    "level_ofi",
]

GRID_NS = 100_000_000  # 100 ms
LEVELS = 10
OFI_WINDOWS_MS = (100, 1_000, 5_000, 30_000)
TFI_WINDOWS_MS = (1_000, 5_000, 30_000)
RV_WINDOWS_S = (10, 60, 300)
TREND_WINDOWS_S = (30, 120, 600)
DEPTH_BANDS_BP = (5, 10)
MS = 1_000_000
S = 1_000_000_000

Level = tuple[float, float]


class LookaheadError(RuntimeError):
    """A sample was requested at or before an event already fed."""


class BookView(Protocol):
    """What the engine reads from a book (``LocalOrderBook`` satisfies it)."""

    def top(self, levels: int) -> tuple[list[Level], list[Level]]: ...

    def notional_within(self, bp: float) -> tuple[float, float]: ...


@dataclass(frozen=True)
class EngineConfig:
    tick_sizes: Mapping[str, float]
    # Level weights for the integrated OFI; fit by PCA on training data only.
    pca_weights: tuple[float, ...] = (1.0,) + (0.0,) * (LEVELS - 1)
    # (imbalance bucket, spread ticks capped at spread_cap) -> adjustment in ticks.
    microprice_table: Mapping[tuple[int, int], float] = field(default_factory=dict)
    imbalance_buckets: int = 10
    spread_cap: int = 5
    # symbol -> the symbol whose OFI feeds it (BTC <-> ETH).
    cross_pairs: Mapping[str, str] = field(default_factory=dict)
    cross_lag_ms: int = 100

    def __post_init__(self) -> None:
        if len(self.pca_weights) != LEVELS:
            raise ValueError(f"pca_weights needs {LEVELS} entries")


def feature_names(cross: bool = True) -> list[str]:
    """Stable column order for models."""
    names: list[str] = []
    for window in OFI_WINDOWS_MS:
        names += [f"ofi_l{level}_{window}ms" for level in range(1, LEVELS + 1)]
        names.append(f"ofi_int_{window}ms")
    names += ["imbalance", "microprice_minus_mid_bp", "spread_ticks", "spread_bp"]
    for window in TFI_WINDOWS_MS:
        names += [f"tfi_{window}ms", f"vwap_to_mid_bp_{window}ms"]
    names += [f"rv_{window}s_bp" for window in RV_WINDOWS_S]
    names += [f"ret_{window}s_bp" for window in TREND_WINDOWS_S]
    for band in DEPTH_BANDS_BP:
        names += [f"depth_bid_{band}bp", f"depth_ask_{band}bp"]
    if cross:
        names += [f"xasset_ofi_l1_{window}ms" for window in OFI_WINDOWS_MS]
    names += ["secs_to_funding", "funding_bucket"]
    return names


def funding_bucket(rate: float) -> int:
    """-2..2 around the 0.01%/8h baseline (1 bp)."""
    bp = rate * 10_000
    if bp <= -1:
        return -2
    if bp < 0:
        return -1
    if bp <= 1:
        return 0
    if bp <= 3:
        return 1
    return 2


def level_ofi(previous: Level | None, current: Level | None, *, bid: bool) -> float:
    """One side's contribution to Cont's OFI at one level, in quote notional.

    Bid side: ``1{Pb >= Pb'} * Qb - 1{Pb <= Pb'} * Qb'`` (primes are the
    previous state). Ask side is the mirror with the sign flipped, so positive
    OFI is buying pressure on both. A level that is missing on either side of
    the change contributes nothing.
    """
    if previous is None or current is None:
        return 0.0
    price, qty = current
    old_price, old_qty = previous
    if bid:
        return (price * qty if price >= old_price else 0.0) - (
            old_price * old_qty if price <= old_price else 0.0
        )
    return (old_price * old_qty if price >= old_price else 0.0) - (
        price * qty if price <= old_price else 0.0
    )


class PrefixSeries:
    """Append-only timestamped vectors with prefix sums.

    ``window(t0, t1)`` sums entries with ``t0 <= t < t1`` in O(log n).
    ``prune`` drops entries older than a horizon and rebases the sums so they
    never grow large enough to lose float precision.
    """

    def __init__(self, width: int) -> None:
        self.width = width
        self.times: list[int] = []
        self._cum: list[list[float]] = [[0.0] * width]

    def __len__(self) -> int:
        return len(self.times)

    def append(self, t_ns: int, values: Sequence[float]) -> None:
        if self.times and t_ns < self.times[-1]:
            raise ValueError("PrefixSeries timestamps must not go backwards")
        last = self._cum[-1]
        self.times.append(t_ns)
        self._cum.append([last[i] + values[i] for i in range(self.width)])

    def window(self, t0: int, t1: int) -> list[float]:
        i0 = bisect_left(self.times, t0)
        i1 = bisect_left(self.times, t1)
        a, b = self._cum[i0], self._cum[i1]
        return [b[i] - a[i] for i in range(self.width)]

    def count(self, t0: int, t1: int) -> int:
        return bisect_left(self.times, t1) - bisect_left(self.times, t0)

    def prune(self, before_ns: int) -> None:
        cut = bisect_left(self.times, before_ns)
        if cut == 0:
            return
        base = self._cum[cut]
        self.times = self.times[cut:]
        self._cum = [[row[i] - base[i] for i in range(self.width)] for row in self._cum[cut:]]


class SymbolState:
    def __init__(self, symbol: str, tick_size: float) -> None:
        self.symbol = symbol
        self.tick_size = tick_size
        self.ofi = PrefixSeries(LEVELS)
        # (signed qty, qty, price*qty)
        self.trades = PrefixSeries(3)
        # grid mids: (t, log mid); returns kept as prefix sums of (r, r^2)
        self._grid_t: list[int] = []
        self._grid_logmid: list[float] = []
        self.returns = PrefixSeries(2)
        self._top: tuple[list[Level], list[Level]] | None = None
        self.book: BookView | None = None
        self.last_event_ns: int | None = None
        self.funding_rate: float | None = None
        self.next_funding_ms: int | None = None

    # --- feeding ----------------------------------------------------------------

    def _touch(self, t_ns: int) -> None:
        if self.last_event_ns is not None and t_ns < self.last_event_ns:
            raise ValueError(f"{self.symbol}: events must be fed in recv_ns order")
        self.last_event_ns = t_ns

    def on_book(self, t_ns: int, book: BookView) -> None:
        """After every applied depth diff: accumulate per-level OFI."""
        self._touch(t_ns)
        bids, asks = book.top(LEVELS)
        if self._top is not None:
            old_bids, old_asks = self._top
            values = [
                level_ofi(_at(old_bids, m), _at(bids, m), bid=True)
                + level_ofi(_at(old_asks, m), _at(asks, m), bid=False)
                for m in range(LEVELS)
            ]
            self.ofi.append(t_ns, values)
        self._top = (bids, asks)
        self.book = book

    def on_book_reset(self, t_ns: int) -> None:
        """The book was discarded: the next state must not be diffed against the old."""
        self._touch(t_ns)
        self._top = None
        self.book = None

    def on_trade(self, t_ns: int, price: float, qty: float, buyer_is_maker: bool) -> None:
        self._touch(t_ns)
        signed = -qty if buyer_is_maker else qty
        self.trades.append(t_ns, (signed, qty, price * qty))

    def on_funding(self, t_ns: int, rate: float, next_funding_ms: int) -> None:
        self._touch(t_ns)
        self.funding_rate = rate
        self.next_funding_ms = next_funding_ms

    # --- reading ----------------------------------------------------------------

    def touch(self) -> tuple[Level, Level] | None:
        if self._top is None:
            return None
        bids, asks = self._top
        if not bids or not asks:
            return None
        return bids[0], asks[0]

    def mid(self) -> float | None:
        touch = self.touch()
        return None if touch is None else (touch[0][0] + touch[1][0]) / 2

    def record_grid_mid(self, t_ns: int) -> None:
        mid = self.mid()
        if mid is None or mid <= 0:
            return
        log_mid = math.log(mid)
        if self._grid_logmid:
            r = log_mid - self._grid_logmid[-1]
            self.returns.append(t_ns, (r, r * r))
        self._grid_t.append(t_ns)
        self._grid_logmid.append(log_mid)

    def grid_log_mid_at_or_before(self, t_ns: int) -> float | None:
        index = bisect_right(self._grid_t, t_ns) - 1
        return self._grid_logmid[index] if index >= 0 else None

    def prune(self, now_ns: int) -> None:
        self.ofi.prune(now_ns - (max(OFI_WINDOWS_MS) + 1_000) * MS)
        self.trades.prune(now_ns - (max(TFI_WINDOWS_MS) + 1_000) * MS)
        horizon = now_ns - (max(max(TREND_WINDOWS_S), max(RV_WINDOWS_S)) + 5) * S
        self.returns.prune(horizon)
        cut = bisect_left(self._grid_t, horizon)
        if cut:
            self._grid_t = self._grid_t[cut:]
            self._grid_logmid = self._grid_logmid[cut:]


def _at(levels: list[Level], index: int) -> Level | None:
    return levels[index] if index < len(levels) else None


class GridClock:
    """Grid times on fixed multiples of ``step_ns``, driven by event time.

    Before feeding an event stamped ``recv_ns``, call ``due(recv_ns)``: it
    returns every grid time ``g`` with ``g <= recv_ns`` not yet sampled. Since
    events arrive in ``recv_ns`` order, everything before ``g`` has been fed
    and nothing at or after it has: the sample is exactly what a replay of the
    recording produces. A quiet-market timer calls ``due(now)`` the same way.
    """

    def __init__(self, step_ns: int = GRID_NS) -> None:
        self.step_ns = step_ns
        self.next_ns: int | None = None

    def due(self, t_ns: int) -> list[int]:
        if self.next_ns is None:
            self.next_ns = (t_ns // self.step_ns + 1) * self.step_ns
            return []
        times: list[int] = []
        while self.next_ns <= t_ns:
            times.append(self.next_ns)
            self.next_ns += self.step_ns
        return times


class MarketState:
    """Every symbol's state, plus the cross-asset wiring."""

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.symbols = {
            symbol: SymbolState(symbol, tick) for symbol, tick in config.tick_sizes.items()
        }
        self._samples = 0

    def __getitem__(self, symbol: str) -> SymbolState:
        return self.symbols[symbol]

    def grid(self, t_ns: int) -> dict[str, dict[str, Any]]:
        """A grid tick: record every symbol's mid at t, then sample them all.

        Volatility and trend read only grid mids, so off-grid samples (trade
        bursts, via :meth:`sample`) cannot change them.
        """
        self._check_lookahead(t_ns)
        for state in self.symbols.values():
            state.record_grid_mid(t_ns)
        self._samples += 1
        if self._samples % 600 == 0:
            for state in self.symbols.values():
                state.prune(t_ns)
        return {symbol: self._features(state, t_ns) for symbol, state in self.symbols.items()}

    def sample(self, symbol: str, t_ns: int) -> dict[str, Any]:
        """The feature vector for ``symbol`` at ``t_ns`` without recording a grid mid."""
        self._check_lookahead(t_ns)
        return self._features(self.symbols[symbol], t_ns)

    def _check_lookahead(self, t_ns: int) -> None:
        for state in self.symbols.values():
            if state.last_event_ns is not None and state.last_event_ns >= t_ns:
                raise LookaheadError(
                    f"{state.symbol} has an event at {state.last_event_ns} >= sample {t_ns}"
                )

    def _features(self, state: SymbolState, t: int) -> dict[str, Any]:
        cfg = self.config
        out: dict[str, Any] = {"symbol": state.symbol, "t_ns": t}
        mid = state.mid()
        out["mid"] = mid

        for window in OFI_WINDOWS_MS:
            levels = state.ofi.window(t - window * MS, t)
            for index, value in enumerate(levels, start=1):
                out[f"ofi_l{index}_{window}ms"] = value
            out[f"ofi_int_{window}ms"] = sum(
                weight * value for weight, value in zip(cfg.pca_weights, levels, strict=True)
            )

        touch = state.touch()
        if touch is None or mid is None:
            for name in ("imbalance", "microprice_minus_mid_bp", "spread_ticks", "spread_bp"):
                out[name] = None
        else:
            (bid, bid_qty), (ask, ask_qty) = touch
            total = bid_qty + ask_qty
            imbalance = bid_qty / total if total > 0 else 0.5
            spread = ask - bid
            spread_ticks = spread / state.tick_size
            out["imbalance"] = imbalance
            out["spread_ticks"] = round(spread_ticks, 6)
            out["spread_bp"] = spread / mid * 10_000
            if cfg.microprice_table:
                bucket = min(int(imbalance * cfg.imbalance_buckets), cfg.imbalance_buckets - 1)
                key = (bucket, min(round(spread_ticks), cfg.spread_cap))
                microprice = mid + cfg.microprice_table.get(key, 0.0) * state.tick_size
            else:
                microprice = imbalance * ask + (1 - imbalance) * bid
            out["microprice_minus_mid_bp"] = (microprice - mid) / mid * 10_000

        for window in TFI_WINDOWS_MS:
            signed, volume, notional = state.trades.window(t - window * MS, t)
            if volume > 0:
                out[f"tfi_{window}ms"] = signed / volume
                vwap = notional / volume
                out[f"vwap_to_mid_bp_{window}ms"] = (vwap - mid) / mid * 10_000 if mid else None
            else:
                out[f"tfi_{window}ms"] = None
                out[f"vwap_to_mid_bp_{window}ms"] = None

        for window in RV_WINDOWS_S:
            # Returns that ended in (t - w, t]: the grid mid at t was just recorded.
            n = state.returns.count(t - window * S + 1, t + 1)
            if n >= 2:
                total_r, total_r2 = state.returns.window(t - window * S + 1, t + 1)
                variance = max(total_r2 / n - (total_r / n) ** 2, 0.0) * n / (n - 1)
                out[f"rv_{window}s_bp"] = math.sqrt(variance) * 10_000
            else:
                out[f"rv_{window}s_bp"] = None

        current = state.grid_log_mid_at_or_before(t)
        for window in TREND_WINDOWS_S:
            past = state.grid_log_mid_at_or_before(t - window * S)
            out[f"ret_{window}s_bp"] = (
                (current - past) * 10_000 if current is not None and past is not None else None
            )

        for band in DEPTH_BANDS_BP:
            if state.book is None or mid is None:
                out[f"depth_bid_{band}bp"] = out[f"depth_ask_{band}bp"] = None
            else:
                bid_notional, ask_notional = state.book.notional_within(band)
                out[f"depth_bid_{band}bp"] = bid_notional
                out[f"depth_ask_{band}bp"] = ask_notional

        partner = cfg.cross_pairs.get(state.symbol)
        if partner is not None and partner in self.symbols:
            other = self.symbols[partner]
            end = t - cfg.cross_lag_ms * MS
            for window in OFI_WINDOWS_MS:
                out[f"xasset_ofi_l1_{window}ms"] = other.ofi.window(end - window * MS, end)[0]

        if state.next_funding_ms is not None:
            out["secs_to_funding"] = state.next_funding_ms / 1000 - t / S
            out["funding_bucket"] = funding_bucket(state.funding_rate or 0.0)
        else:
            out["secs_to_funding"] = None
            out["funding_bucket"] = None
        return out
