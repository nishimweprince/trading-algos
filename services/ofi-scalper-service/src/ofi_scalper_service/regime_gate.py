"""The deterministic regime gate (plan §1.6), evaluated on the slow loop.

It can only switch trading off for a symbol, raise the entry-threshold
multiplier, or shrink the inventory multiplier. It never touches the hard
limits in ``risk``. Rules, each disabled when its setting is empty:

- **Funding blackout**: within ``funding_blackout_minutes`` either side of a
  funding timestamp -> off.
- **Liquidation burst**: liquidation notional over the last 60 s at or above
  ``liq_burst_usd`` -> off.
- **Volatility**: ``rv_60s_bp`` (std of grid mid returns over 60 s, in bp per
  grid step) at or above ``vol_1m_bp`` -> off.
- **Spread**: the current spread at or above the ``spread_pctl`` percentile of
  its own last hour -> threshold x1.5, inventory x0.5.

An optional Jev adapter may later sit beside this, never in front of it.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import deque
from dataclasses import dataclass, field
from typing import Any

__all__ = ["GateDecision", "RegimeGate"]

LIQ_WINDOW_NS = 60 * 1_000_000_000
MIN_SPREAD_HISTORY = 600  # one minute of 100 ms samples


@dataclass(frozen=True, slots=True)
class GateDecision:
    on: bool
    threshold_mult: float = 1.0
    max_inventory_mult: float = 1.0
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "on": self.on,
            "threshold_mult": self.threshold_mult,
            "max_inventory_mult": self.max_inventory_mult,
            "reasons": list(self.reasons),
        }


@dataclass
class _SymbolHistory:
    spreads: deque[float]
    liquidations: deque[tuple[int, float]] = field(default_factory=deque)
    next_funding_ms: int | None = None
    prev_funding_ms: int | None = None


class RegimeGate:
    def __init__(
        self,
        *,
        funding_blackout_minutes: float,
        spread_pctl: float | None,
        vol_1m_bp: float | None,
        liq_burst_usd: float | None,
        history_samples: int = 36_000,
    ) -> None:
        self.funding_blackout_ms = int(funding_blackout_minutes * 60_000)
        self.spread_pctl = spread_pctl
        self.vol_1m_bp = vol_1m_bp
        self.liq_burst_usd = liq_burst_usd
        self._history_samples = history_samples
        self._symbols: dict[str, _SymbolHistory] = {}
        self.last: dict[str, GateDecision] = {}

    @classmethod
    def from_settings(cls, settings: Any) -> RegimeGate:
        return cls(
            funding_blackout_minutes=settings.funding_blackout_minutes,
            spread_pctl=settings.gate_spread_pctl,
            vol_1m_bp=settings.gate_vol_1m_bp,
            liq_burst_usd=settings.gate_liq_burst_usd,
        )

    def _history(self, symbol: str) -> _SymbolHistory:
        if symbol not in self._symbols:
            self._symbols[symbol] = _SymbolHistory(deque(maxlen=self._history_samples))
        return self._symbols[symbol]

    def observe(self, features: dict[str, Any]) -> None:
        """Every grid sample: remember the spread and the funding schedule."""
        history = self._history(features["symbol"])
        spread = features.get("spread_bp")
        if spread is not None:
            history.spreads.append(spread)
        secs = features.get("secs_to_funding")
        if secs is not None:
            funding_ms = round(features["t_ns"] / 1e6 + secs * 1000)
            # Funding times move in whole intervals; a jump means one just passed.
            if (
                history.next_funding_ms is not None
                and funding_ms - history.next_funding_ms > 60_000
            ):
                history.prev_funding_ms = history.next_funding_ms
            history.next_funding_ms = funding_ms

    def on_liquidation(self, symbol: str, t_ns: int, notional_usd: float) -> None:
        self._history(symbol).liquidations.append((t_ns, notional_usd))

    def decide(self, features: dict[str, Any]) -> GateDecision:
        symbol = features["symbol"]
        t_ns = features["t_ns"]
        history = self._history(symbol)
        reasons: list[str] = []
        on = True
        threshold_mult = inventory_mult = 1.0

        if self.funding_blackout_ms:
            t_ms = t_ns / 1e6
            for funding_ms in (history.next_funding_ms, history.prev_funding_ms):
                if funding_ms is not None and abs(funding_ms - t_ms) <= self.funding_blackout_ms:
                    on = False
                    reasons.append("funding_blackout")
                    break

        if self.liq_burst_usd is not None:
            while history.liquidations and history.liquidations[0][0] < t_ns - LIQ_WINDOW_NS:
                history.liquidations.popleft()
            burst = sum(notional for _, notional in history.liquidations)
            if burst >= self.liq_burst_usd:
                on = False
                reasons.append("liquidation_burst")

        if self.vol_1m_bp is not None:
            vol = features.get("rv_60s_bp")
            if vol is not None and vol >= self.vol_1m_bp:
                on = False
                reasons.append("volatility")

        if self.spread_pctl is not None:
            spread = features.get("spread_bp")
            if spread is not None and len(history.spreads) >= MIN_SPREAD_HISTORY:
                ordered = sorted(history.spreads)
                rank = bisect_left(ordered, spread) / len(ordered) * 100
                if rank >= self.spread_pctl:
                    threshold_mult, inventory_mult = 1.5, 0.5
                    reasons.append("wide_spread")

        decision = GateDecision(on, threshold_mult, inventory_mult, tuple(reasons))
        self.last[symbol] = decision
        return decision
