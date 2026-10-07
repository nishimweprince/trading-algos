"""Shadow parameter sets: other variants of the rule, decided live, never ordered.

``decide()`` is pure, so every book update can also be put to a list of
variants (``PK_SHADOW_VARIANTS``) at the cost of one call each. Each variant has
its own first-crossing-decides state per window; once it is consumed it costs
nothing more until the roll. No variant places an order or touches the ledger.

Records (``journal``):

- ``shadow``: one per variant per window at the roll, with the decision
  (``action`` ``none`` when the variant never fired), the touch and the
  shares offered at or below the entry price at the decision.
- ``shadow_settle``: one per consumed variant once Polymarket resolves the
  window: the outcome and the net ¢/share at the touch, after the fee.

The live rule is always evaluated alongside as variant ``live``, so a variant
is compared with the live parameters on the same touch-price basis.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from .book import Quote
from .decision import Decision, StrategyParams, decide
from .fees import taker_fee

__all__ = [
    "DEFAULT_VARIANTS",
    "US_NIGHT_UTC",
    "Shadow",
    "Variant",
    "parse_variants",
    "shadow_table",
]

# 01:00-04:59 ET, Kalshi's thinnest hours (EDT; one hour later in winter).
US_NIGHT_UTC = [5, 6, 7, 8, 9, 10]

# PLAN 1.2's starting set.
DEFAULT_VARIANTS = json.dumps(
    [
        {"name": "t08", "gap": 0.08},
        # The rule before epoch 5 (gap 0.10), kept for comparison with 0.15.
        {"name": "t10", "gap": 0.10},
        {"name": "t12", "gap": 0.12},
        {"name": "t15", "gap": 0.15},
        {"name": "t20", "gap": 0.20},
        {"name": "early120", "early": 120},
        {"name": "kspread05", "max_kalshi_spread": 0.05},
        {"name": "cap50", "cap": 0.50},
        # Pre-registered for the live holdout (research/PREREG.md, part 2).
        {"name": "pspread05", "max_poly_spread": 0.05},
        {"name": "pspread10", "max_poly_spread": 0.10},
        # After epoch 4 (0 wins in 16): pre-registered in research/PREREG.md, part 3.
        {"name": "kext", "kalshi_extreme": True},
        {"name": "kext_t15", "kalshi_extreme": True, "gap": 0.15},
        {"name": "floor15", "min_px": 0.15},
        {"name": "usnight", "skip_hours_utc": US_NIGHT_UTC},
        # After epoch 5's asset and latency read: pre-registered in research/PREREG.md,
        # part 4. A Polymarket spread of 1¢ or less (+9.1¢ on Helix discovery), a
        # cheap leg whose latest frame arrived within 250 ms, and 0.20 under a 0.50 cap.
        {"name": "pspread01", "max_poly_spread": 0.01},
        {"name": "plag250", "max_poly_lag_ms": 250},
        {"name": "t20_cap50", "gap": 0.20, "cap": 0.50},
    ]
)
_FIELDS = {
    "gap": "gap_threshold",
    "cap": "max_entry_px",
    "early": "entry_early_s",
    "late": "entry_late_s",
    "max_kalshi_spread": "max_kalshi_spread",
    "max_poly_spread": "max_poly_spread",
    "min_px": "min_entry_px",
    "kalshi_extreme": "kalshi_extreme",
    "skip_hours_utc": "skip_hours_utc",
    "max_poly_lag_ms": "max_poly_lag_ms",
}


@dataclass(frozen=True, slots=True)
class Variant:
    name: str
    params: StrategyParams


def parse_variants(text: str, base: StrategyParams) -> tuple[Variant, ...]:
    """``[{name, gap, cap, early, late, max_kalshi_spread, max_poly_spread, min_px,
    kalshi_extreme, skip_hours_utc, max_poly_lag_ms}, ...]``; a missing key
    keeps the live value. An empty string means no variants."""
    if not text.strip():
        return ()
    items = json.loads(text)
    if not isinstance(items, list):
        raise ValueError("PK_SHADOW_VARIANTS must be a JSON list")
    out, names = [], set()
    for item in items:
        name = str(item.get("name") or "")
        if not name or name in names or name == "live":
            raise ValueError(f"shadow variant name {name!r} is empty, repeated or reserved")
        unknown = set(item) - set(_FIELDS) - {"name"}
        if unknown:
            raise ValueError(f"shadow variant {name}: unknown keys {sorted(unknown)}")
        changes = {_FIELDS[k]: v for k, v in item.items() if k in _FIELDS}
        if "skip_hours_utc" in changes:
            changes["skip_hours_utc"] = tuple(int(h) for h in changes["skip_hours_utc"])
        names.add(name)
        out.append(Variant(name, replace(base, **changes)))
    return tuple(out)


@dataclass
class _Fired:
    record: dict[str, Any]
    fee_rate: float
    fee_exponent: float


class Shadow:
    def __init__(self, variants: tuple[Variant, ...], live: StrategyParams) -> None:
        self.variants = (Variant("live", live), *variants) if variants else ()
        # (asset, open_unix) -> variant name -> what it decided in that window
        self._fired: dict[tuple[str, int], dict[str, _Fired]] = {}
        self._done: dict[tuple[str, int], set[str]] = {}

    def __bool__(self) -> bool:
        return bool(self.variants)

    def evaluate(
        self,
        key: tuple[str, int],
        quotes: tuple[Quote, Quote, Quote],
        sec_into: float,
        recv_ns: int,
        depth: Any,
        fee: tuple[float, float],
        lags: tuple[float | None, float | None] | None = None,
    ) -> None:
        """Run every variant not yet consumed in this window. ``depth(buy_yes, px)``
        returns the shares offered at or below ``px`` on that leg; ``lags`` is the
        transport lag of the YES and NO books' latest frames (``decide``)."""
        done = self._done.setdefault(key, set())
        if len(done) == len(self.variants):
            return
        k, y, n = quotes
        utc_hour = int(key[1] + sec_into) // 3600 % 24
        for variant in self.variants:
            if variant.name in done:
                continue
            d: Decision = decide(k, y, n, variant.params, sec_into, False, utc_hour, lags)
            if not d.consumes_window:
                continue
            done.add(variant.name)
            self._fired.setdefault(key, {})[variant.name] = _Fired(
                {
                    "variant": variant.name,
                    "action": d.action.value,
                    "reason": d.reason,
                    "t_ns": recv_ns,
                    "sec_into": round(sec_into, 3),
                    "gap": round(d.gap or 0.0, 4),
                    "side": "yes" if d.buy_yes else "no",
                    "entry_px": d.entry_px,
                    "depth_at_limit": None
                    if d.entry_px is None
                    else round(depth(bool(d.buy_yes), d.entry_px), 2),
                    "kalshi": k.as_dict(),
                    "poly_yes": y.as_dict(),
                    "poly_no": n.as_dict(),
                },
                *fee,
            )

    def close(self, key: tuple[str, int]) -> list[dict[str, Any]]:
        """The window's ``shadow`` records, one per variant, at the roll."""
        if not self.variants:
            return []
        fired = self._fired.get(key, {})
        self._done.pop(key, None)
        asset, open_unix = key
        out = []
        for variant in self.variants:
            body = fired[variant.name].record if variant.name in fired else {
                "variant": variant.name, "action": "none",
            }  # fmt: skip
            out.append({"type": "shadow", "asset": asset, "open_unix": open_unix, **body})
        return out

    def settle(self, key: tuple[str, int], outcome: str) -> list[dict[str, Any]]:
        """``shadow_settle`` records for the window's consumed variants; forgets it."""
        fired = self._fired.pop(key, {})
        asset, open_unix = key
        out = []
        for name, f in fired.items():
            px = f.record["entry_px"]
            if px is None:
                continue
            win = 1.0 if f.record["side"] == outcome else 0.0
            net = 100 * (win - px - taker_fee(px, 1.0, f.fee_rate, f.fee_exponent))
            out.append(
                {
                    "type": "shadow_settle",
                    "asset": asset,
                    "open_unix": open_unix,
                    "variant": name,
                    "action": f.record["action"],
                    "side": f.record["side"],
                    "entry_px": px,
                    "outcome": outcome,
                    "win": win,
                    "net_cents_touch": round(net, 3),
                }
            )
        return out

    def forget_before(self, open_unix: int) -> None:
        for key in [k for k in self._fired if k[1] < open_unix]:
            self._fired.pop(key, None)
        for key in [k for k in self._done if k[1] < open_unix]:
            self._done.pop(key, None)


def _cluster_se(rows: list[dict[str, Any]], mean: float) -> float | None:
    """Standard error of the mean net with entries clustered by window start.

    The assets move together inside a window, so entries in one window are not
    independent: epoch 4's 16 losses came from 4 windows. With one cluster per
    ``open_unix`` this is the usual sandwich estimator with the C/(C-1) factor.
    """
    sums: dict[int, float] = {}
    for r in rows:
        sums[r["open_unix"]] = sums.get(r["open_unix"], 0.0) + r["net_cents_touch"] - mean
    c, n = len(sums), len(rows)
    if c < 2:
        return None
    return (c / (c - 1) * sum(v * v for v in sums.values())) ** 0.5 / n


def _session_stats(rows: list[dict[str, Any]], night: bool) -> dict[str, Any]:
    part = [r for r in rows if (r["open_unix"] // 3600 % 24 in US_NIGHT_UTC) == night]
    n = len(part)
    return {
        "n": n,
        "win_rate": None if not n else sum(r["win"] for r in part) / n,
        "net_cents": None if not n else sum(r["net_cents_touch"] for r in part) / n,
    }


def shadow_table(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per variant: windows, crossings, entries, settled n, win rate, average
    price, net ¢/share at the touch and its standard error, the number of
    distinct windows the entries came from with the window-clustered s.e., and
    the same n, win rate and net split by US night (``US_NIGHT_UTC``) and day.
    Entries only (a skip above the cap is a crossing, not a trade)."""
    windows: dict[str, int] = {}
    crossings: dict[str, int] = {}
    settled: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        kind = r.get("type")
        if kind == "shadow":
            v = r["variant"]
            windows[v] = windows.get(v, 0) + 1
            if r.get("action") in ("enter", "skip"):
                crossings[v] = crossings.get(v, 0) + 1
        elif kind == "shadow_settle" and r.get("action") == "enter":
            settled.setdefault(r["variant"], []).append(r)
    out = {}
    for v in sorted(set(windows) | set(settled), key=lambda x: (x != "live", x)):
        rows = settled.get(v, [])
        nets = [r["net_cents_touch"] for r in rows]
        n = len(rows)
        se = se_cluster = None
        if n > 1:
            mean = sum(nets) / n
            var = sum((x - mean) ** 2 for x in nets) / (n - 1)
            se = (var / n) ** 0.5
            se_cluster = _cluster_se(rows, mean)
        out[v] = {
            "windows": windows.get(v, 0),
            "crossings": crossings.get(v, 0),
            "n": n,
            "win_rate": None if not n else sum(r["win"] for r in rows) / n,
            "avg_px": None if not n else sum(r["entry_px"] for r in rows) / n,
            "net_cents": None if not n else sum(nets) / n,
            "se_cents": se,
            "entry_windows": len({r["open_unix"] for r in rows}),
            "se_cluster_cents": se_cluster,
            "us_night": _session_stats(rows, True),
            "us_day": _session_stats(rows, False),
        }
    return out
