"""Flow guard: which sides short-window order flow is running against.

On demo, entries filled within 500 ms of the signal were run over (the mid
30 s later was 0.8 bp against them; fills after 3 s were 3.7 bp for them).
A resting bid fills fastest exactly when selling hits it. The guard spots
that flow from the same features the model sees and marks the side
``against``; the policy then neither enters nor keeps resting on it, and a
maker exit chase gives up for the market close (``policy.py``).

Selling pressure (``"buy"`` against) when all the configured conditions hold:

- ``ofi_int_<w>ms`` z-score <= ``-guard_z``, against its own exponentially
  weighted mean and variance on event time (as the rule model's signal);
- ``tfi_<w>ms`` <= ``-guard_tfi`` (if set);
- touch imbalance <= ``1 - guard_imb`` (if set).

Buying pressure mirrors it. Off unless the model's policy sets ``guard_z``.
"""

from __future__ import annotations

from typing import Any

from .policy import PolicyParams
from .rules import EwStats, ew_update

__all__ = ["FlowGuard"]

# The guard needs this long of its signal's history before it says anything.
WARMUP_S = 300.0


class FlowGuard:
    def __init__(self, params: PolicyParams) -> None:
        self.params = params
        self.enabled = params.guard_z is not None
        self.ofi = f"ofi_int_{params.guard_window_ms}ms"
        self.tfi = f"tfi_{params.guard_window_ms}ms"
        self.stats: dict[str, EwStats] = {}
        self.flagged: dict[str, int] = {"buy": 0, "sell": 0}

    def against(self, sample: dict[str, Any]) -> frozenset[str]:
        """The sides flow runs against at this sample (grid samples update the stats)."""
        if not self.enabled:
            return frozenset()
        x = sample.get(self.ofi)
        if x is None:
            return frozenset()
        x = float(x)
        symbol, t_ns = str(sample["symbol"]), int(sample["t_ns"])
        stats = self.stats.setdefault(symbol, EwStats())
        out: frozenset[str] = frozenset()
        if stats.warm(t_ns, WARMUP_S):
            out = self._judge(stats.z(x), sample)
        if sample.get("trigger", "grid") == "grid":
            ew_update(stats, x, t_ns, self.params.guard_halflife_s)
        for side in out:
            self.flagged[side] += 1
        return out

    def _judge(self, z: float, sample: dict[str, Any]) -> frozenset[str]:
        p = self.params
        assert p.guard_z is not None
        tfi = sample.get(self.tfi)
        imbalance = sample.get("imbalance")
        sides: set[str] = set()
        for side, sign in (("buy", -1.0), ("sell", 1.0)):
            # "buy" is against when flow sells: z, trade flow and imbalance all low.
            if sign * z < p.guard_z:
                continue
            if p.guard_tfi is not None and (tfi is None or sign * float(tfi) < p.guard_tfi):
                continue
            if p.guard_imb is not None:
                if imbalance is None:
                    continue
                book = float(imbalance) if side == "sell" else 1 - float(imbalance)
                if book < p.guard_imb:
                    continue
            sides.add(side)
        return frozenset(sides)
