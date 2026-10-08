"""A rule scorer: the hot path's model slot filled by a fixed OFI rule, no training.

For demo trading and shadow trading on the live feed before a trained model
passes its gates (``OFI_ALLOW_UNGATED``). It produces the same ``Scores`` the
CatBoost scorer does, so policy, risk, bridge and records run unchanged. A rule
model is ungated: it never loads in a mode that needs a gates pass, and its
trades are labelled ``ungated`` so they stay out of the speed-run record.

The rule, per symbol, on 100 ms grid samples:

- ``z``: the signal feature (default ``ofi_int_5000ms``) against its own
  exponentially weighted mean and variance (``halflife_s``), scored before the
  sample updates them. No score until ``warmup_s`` of samples.
- **Up** when ``z >= z_entry``, the touch imbalance is at least
  ``imbalance_min``, trade flow (``tfi_feature``) agrees in sign, and the spread
  is at most ``max_spread_ticks``. **Down** is the mirror image.
- A firing is held for ``hold_ms`` so the resting entry is not cancelled as
  "decay" on the next sample; the opposite firing replaces it at once.

While held, the side scores ``confidence`` and the rest goes to ``none``.
Otherwise ``none`` is 1. Off-grid (burst) samples are scored against the
current state but do not update it, so the rule sees the same statistics in a
replay as live.

    ofi-rules-model --profile dev --version rules-v2   # <OFI_MODEL_DIR>/rules-v2
    ofi-rules-model --profile dev --version rules-v2 --z-entry 4 --barrier-bp 15
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .policy import Scores

__all__ = [
    "DEFAULT_POLICY",
    "RuleParams",
    "RuleScorer",
    "main",
]

LATENCY_RESERVOIR = 2000
NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000

# Wide enough that the maker round trip (~4 bp) plus the buffer is well inside
# the barrier, with a minute-scale horizon (6 bp at 1-30 s left no room).
DEFAULT_POLICY: dict[str, Any] = {
    "horizon_s": 60.0,
    "threshold": 0.5,
    "barrier_bp": 12.0,
    "buffer_bp": 2.0,
    "entry_timeout_ms": 1500,
    "time_stop_mult": 2.0,
    "reentry_cooldown_ms": 5000,
}


@dataclass(frozen=True, slots=True)
class RuleParams:
    signal: str = "ofi_int_5000ms"
    z_entry: float = 3.5
    halflife_s: float = 300.0
    warmup_s: float = 300.0
    imbalance_min: float = 0.6
    tfi_feature: str | None = "tfi_5000ms"
    max_spread_ticks: float = 2.0
    hold_ms: int = 1500
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if self.z_entry <= 0 or self.halflife_s <= 0 or self.warmup_s < 0:
            raise ValueError("rules: z_entry and halflife_s must be > 0, warmup_s >= 0")
        if not 0.5 <= self.imbalance_min < 1:
            raise ValueError("rules: imbalance_min must be in [0.5, 1)")
        if not 0 < self.confidence <= 1 or self.hold_ms <= 0 or self.max_spread_ticks <= 0:
            raise ValueError("rules: confidence in (0, 1], hold_ms and max_spread_ticks > 0")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RuleParams:
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(f"unknown rules fields: {', '.join(unknown)}")
        return cls(**raw)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def features(self) -> list[str]:
        names = [self.signal, "imbalance", "spread_ticks"]
        if self.tfi_feature:
            names.append(self.tfi_feature)
        return names


@dataclass
class _SymbolState:
    mean: float = 0.0
    var: float = 0.0
    first_ns: int | None = None
    last_ns: int | None = None
    held_side: str | None = None
    held_until_ns: int = 0


@dataclass
class RuleScorer:
    """Feature sample -> ``Scores`` by the rule above; the ``Scorer`` interface."""

    params: RuleParams
    states: dict[str, _SymbolState] = field(default_factory=dict)
    latency_us: deque[float] = field(default_factory=lambda: deque(maxlen=LATENCY_RESERVOIR))
    fired: dict[str, int] = field(default_factory=dict)

    @property
    def features(self) -> list[str]:
        return self.params.features

    def score(self, sample: dict[str, Any]) -> Scores | None:
        """None when a feature is missing: no score, no trade (as the model does)."""
        values = {name: sample.get(name) for name in self.params.features}
        if any(value is None for value in values.values()):
            return None
        started = time.perf_counter()
        symbol, t_ns = str(sample["symbol"]), int(sample["t_ns"])
        state = self.states.setdefault(symbol, _SymbolState())
        x = float(values[self.params.signal])  # type: ignore[arg-type]
        warm = (
            state.first_ns is not None
            and t_ns - state.first_ns >= self.params.warmup_s * NS_PER_S
            and state.var > 0
        )
        side = self._fire(state, x, values) if warm else None
        if sample.get("trigger", "grid") == "grid":
            self._update(state, x, t_ns)
        if side is not None:
            state.held_side, state.held_until_ns = side, t_ns + self.params.hold_ms * NS_PER_MS
            self.fired[side] = self.fired.get(side, 0) + 1
        scores = Scores(up=0.0, down=0.0, none=1.0)
        if state.held_side is not None and t_ns < state.held_until_ns:
            c = self.params.confidence
            scores = (
                Scores(up=c, down=0.0, none=1 - c)
                if state.held_side == "up"
                else Scores(up=0.0, down=c, none=1 - c)
            )
        self.latency_us.append((time.perf_counter() - started) * 1e6)
        return scores

    def _fire(self, state: _SymbolState, x: float, values: dict[str, Any]) -> str | None:
        p = self.params
        z = (x - state.mean) / math.sqrt(state.var)
        imbalance = float(values["imbalance"])
        if float(values["spread_ticks"]) > p.max_spread_ticks:
            return None
        flow = float(values[p.tfi_feature]) if p.tfi_feature else None
        if z >= p.z_entry and imbalance >= p.imbalance_min and (flow is None or flow > 0):
            return "up"
        if z <= -p.z_entry and imbalance <= 1 - p.imbalance_min and (flow is None or flow < 0):
            return "down"
        return None

    def _update(self, state: _SymbolState, x: float, t_ns: int) -> None:
        """Exponentially weighted mean and variance, decaying on event time."""
        if state.first_ns is None or state.last_ns is None:
            state.first_ns = state.last_ns = t_ns
            state.mean, state.var = x, 0.0
            return
        dt_s = max(t_ns - state.last_ns, 0) / NS_PER_S
        state.last_ns = t_ns
        alpha = 1.0 - 0.5 ** (dt_s / self.params.halflife_s)
        diff = x - state.mean
        increment = alpha * diff
        state.mean += increment
        state.var = (1 - alpha) * (state.var + diff * increment)

    def latency(self) -> dict[str, float] | None:
        if not self.latency_us:
            return None
        ordered = sorted(self.latency_us)

        def pick(q: float) -> float:
            return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 1)

        return {"p50_us": pick(0.5), "p99_us": pick(0.99), "n": len(ordered)}


# --- CLI ---------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    from .config import load_settings
    from .model import write_rules_model

    parser = argparse.ArgumentParser(
        prog="ofi-rules-model", description="Write a rule model directory (ungated)."
    )
    parser.add_argument("--profile", default=None)
    parser.add_argument("--version", default="rules-v2")
    parser.add_argument("--model-dir", type=Path, default=None)
    defaults = RuleParams()
    for name in ("z_entry", "halflife_s", "warmup_s", "imbalance_min", "max_spread_ticks"):
        parser.add_argument(
            f"--{name.replace('_', '-')}", type=float, default=getattr(defaults, name)
        )
    parser.add_argument("--signal", default=defaults.signal)
    parser.add_argument("--hold-ms", type=int, default=defaults.hold_ms)
    for name in ("horizon_s", "threshold", "barrier_bp", "buffer_bp", "time_stop_mult"):
        parser.add_argument(f"--{name.replace('_', '-')}", type=float, default=DEFAULT_POLICY[name])
    parser.add_argument("--entry-timeout-ms", type=int, default=DEFAULT_POLICY["entry_timeout_ms"])
    parser.add_argument(
        "--reentry-cooldown-ms", type=int, default=DEFAULT_POLICY["reentry_cooldown_ms"]
    )
    args = parser.parse_args(argv)

    model_dir = args.model_dir
    if model_dir is None:
        model_dir = load_settings(args.profile).model_dir
    rules = RuleParams(
        signal=args.signal,
        z_entry=args.z_entry,
        halflife_s=args.halflife_s,
        warmup_s=args.warmup_s,
        imbalance_min=args.imbalance_min,
        max_spread_ticks=args.max_spread_ticks,
        hold_ms=args.hold_ms,
    )
    policy = {
        "horizon_s": args.horizon_s,
        "threshold": args.threshold,
        "barrier_bp": args.barrier_bp,
        "buffer_bp": args.buffer_bp,
        "entry_timeout_ms": args.entry_timeout_ms,
        "time_stop_mult": args.time_stop_mult,
        "reentry_cooldown_ms": args.reentry_cooldown_ms,
    }
    try:
        path = write_rules_model(model_dir, args.version, rules=rules.as_dict(), policy=policy)
    except FileExistsError:
        print(f"{model_dir / args.version} exists; pick a new --version", file=sys.stderr)
        return 1
    print(f"wrote {path} (ungated rule model)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
