"""Support/resistance zones from slow- and fast-TF swings.

Rebuilt from scratch on every fast or slow close (cheap at these sizes); what
cannot be rebuilt from swings alone (role flips, breaks, deep retests, later
touches) is carried from the previous zone that shares the most seed swings.

1. Seeds: confirmed swings, slow TF within its lookback, fast TF within its.
2. Width ``w = clamp(pivot body, 0.25 * ATR(TF), 0.6 * ATR(TF))``; a high seed
   gives ``[H - w, H]``, a low seed ``[L, L + w]``.
3. Merge zones that overlap or whose midpoints are within 0.5 * ATR14(slow); a
   merged zone wider than 1.0 * ATR14(slow) is shrunk to that width around the
   mean of its seed midpoints.
4. Strength 0-100 (the spec names the parts and weights; the scales are ours):

   ==============  ===  =========================================================
   touches          25  25 * min(seeds + later touches, 4) / 4
   timeframe        20  20 with a slow-TF seed, else 10
   departure        20  20 * min(largest move away from a seed / ATR14(slow), 3) / 3
   recency          15  15 * max(0, 1 - days since newest seed pivot / slow lookback)
   HVN              10  a volume-profile high-volume node inside the zone
   role flip        10  the zone has flipped role at least once
   ==============  ===  =========================================================

5. Broken after a fast close beyond the zone by 0.25 * ATR14(fast); it then flips
   role. Deleted after two opposite breaks within 30 days, or three deep
   retests (a visit penetrating over 50% of the width from the role side).
   A deleted zone's seeds never seed a zone again.
6. Keep the ``keep_per_side`` strongest zones above and below price, plus any
   zone containing price.

A new zone's role comes from its position: below the close is support, above
is resistance. After that only breaks change it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field

from zone_trend.config import Zones as ZonesCfg
from zone_trend.types import TF, Swing, Zone

__all__ = ["ZoneBook"]

DAY_MS = 86_400_000
OPPOSITE_BREAK_WINDOW_MS = 30 * DAY_MS
MAX_DEEP_RETESTS = 3
DEEP_FRACTION = 0.5


def seed_key(sw: Swing) -> str:
    return f"{sw.tf.value}:{sw.kind}:{sw.pivot_time}"


@dataclass
class _Seed:
    key: str
    tf: TF
    lo: float
    hi: float
    pivot_time: int
    t_conf: int
    departure: float  # in ATR14(slow)

    @property
    def mid(self) -> float:
        return (self.lo + self.hi) / 2


def _seeds(
    swings: list[Swing],
    lookback_ms: int,
    now: int,
    close: float,
    atr_slow: float,
    cfg: ZonesCfg,
) -> list[_Seed]:
    out = []
    for idx, sw in enumerate(swings):
        if sw.t_conf > now or sw.pivot_time < now - lookback_ms:
            continue
        w = min(max(sw.body, cfg.width_atr.min * sw.atr), cfg.width_atr.max * sw.atr)
        lo, hi = (sw.price - w, sw.price) if sw.kind == "high" else (sw.price, sw.price + w)
        nxt = next((s for s in swings[idx + 1 :] if s.t_conf <= now), None)
        away = abs(sw.price - (nxt.price if nxt else close))
        out.append(_Seed(seed_key(sw), sw.tf, lo, hi, sw.pivot_time, sw.t_conf, away / atr_slow))
    return out


def _merge(seeds: list[_Seed], atr_slow: float, cfg: ZonesCfg) -> list[list[_Seed]]:
    clusters: list[list[_Seed]] = []
    for sd in sorted(seeds, key=lambda s: (s.mid, s.key)):
        if clusters:
            cur = clusters[-1]
            lo = min(s.lo for s in cur)
            hi = max(s.hi for s in cur)
            if sd.lo <= hi or abs(sd.mid - (lo + hi) / 2) <= cfg.merge_atr_slow * atr_slow:
                cur.append(sd)
                continue
        clusters.append([sd])
    return clusters


@dataclass
class ZoneBook:
    cfg: ZonesCfg
    slow_tf: TF = TF.D1
    fast_tf: TF = TF.H4
    zones: list[Zone] = field(default_factory=list)
    deleted_seeds: set[str] = field(default_factory=set)

    # -- rebuild ---------------------------------------------------------------
    def rebuild(
        self,
        now: int,
        sw_slow: list[Swing],
        sw_fast: list[Swing],
        atr_slow: float,
        close: float,
        hvn: Iterable[float] = (),
    ) -> list[Zone]:
        if not atr_slow > 0:
            self.zones = []
            return self.zones
        cfg = self.cfg
        slow_days = cfg.lookback_days.days(self.slow_tf)
        fast_days = cfg.lookback_days.days(self.fast_tf)
        seeds = (
            _seeds(sw_slow, slow_days * DAY_MS, now, close, atr_slow, cfg) if cfg.use_slow else []
        )
        seeds += _seeds(sw_fast, fast_days * DAY_MS, now, close, atr_slow, cfg)
        seeds = [s for s in seeds if s.key not in self.deleted_seeds]
        hvn = list(hvn)
        built = [self._zone(c, now, close, atr_slow, hvn) for c in _merge(seeds, atr_slow, cfg)]
        self.zones = self._keep(built, close)
        return self.zones

    def _zone(
        self, cluster: list[_Seed], now: int, close: float, atr_slow: float, hvn: list[float]
    ) -> Zone:
        cfg = self.cfg
        lo = min(s.lo for s in cluster)
        hi = max(s.hi for s in cluster)
        cap = cfg.max_width_atr_slow * atr_slow
        if hi - lo > cap:
            centre = sum(s.mid for s in cluster) / len(cluster)
            lo, hi = centre - cap / 2, centre + cap / 2
        keys = frozenset(s.key for s in cluster)
        zid = hashlib.sha1("|".join(sorted(keys)).encode()).hexdigest()[:12]
        origin = {s.tf for s in cluster}
        prev = self._ancestor(keys)
        if prev is not None:
            zone = Zone(
                id=zid,
                tf_origin=origin,
                lo=lo,
                hi=hi,
                role=prev.role,
                strength=0.0,
                touches=0,
                created=prev.created,
                broken_at=prev.broken_at,
                flips=prev.flips,
                deep_retests=prev.deep_retests,
                breaks=list(prev.breaks),
                seeds=keys,
                extra_touches=prev.extra_touches,
                in_visit=prev.in_visit,
                visit_deep=prev.visit_deep,
            )
        else:
            zone = Zone(
                id=zid,
                tf_origin=origin,
                lo=lo,
                hi=hi,
                role="support" if (lo + hi) / 2 < close else "resistance",
                strength=0.0,
                touches=0,
                created=min(s.t_conf for s in cluster),
                seeds=keys,
            )
        zone.touches = len(cluster) + zone.extra_touches
        newest = max(s.pivot_time for s in cluster)
        slow_days = cfg.lookback_days.days(self.slow_tf)
        zone.strength = (
            25 * min(zone.touches, 4) / 4
            + (20 if self.slow_tf in zone.tf_origin else 10)
            + 20 * min(max(s.departure for s in cluster), 3) / 3
            + 15 * max(0.0, 1 - (now - newest) / (slow_days * DAY_MS))
            + (10 if any(lo <= p <= hi for p in hvn) else 0)
            + (10 if zone.flips else 0)
        )
        return zone

    def _ancestor(self, keys: frozenset[str]) -> Zone | None:
        best, best_n = None, 0
        for z in self.zones:
            n = len(keys & z.seeds)
            if n > best_n or (n == best_n and n and z.id < best.id):
                best, best_n = z, n
        return best

    def _keep(self, zones: list[Zone], close: float) -> list[Zone]:
        n = self.cfg.keep_per_side
        above = sorted((z for z in zones if z.lo > close), key=lambda z: (-z.strength, z.id))
        below = sorted((z for z in zones if z.hi < close), key=lambda z: (-z.strength, z.id))
        inside = [z for z in zones if z.lo <= close <= z.hi]
        return sorted(above[:n] + below[:n] + inside, key=lambda z: (z.lo, z.id))

    # -- fast close ----------------------------------------------------------------
    def mark_breaks(
        self, t: int, high: float, low: float, close: float, atr_fast: float
    ) -> list[tuple[str, str]]:
        """Apply one closed fast-TF bar: breaks, flips, touches, deep retests, deletions.

        Returns ``(zone id, event)`` pairs: ``break_up``, ``break_down``, ``deleted``.
        """
        events: list[tuple[str, str]] = []
        b = self.cfg.break_atr_fast * atr_fast
        keep = []
        for z in self.zones:
            broke = None
            if z.role == "resistance" and close > z.hi + b:
                broke, z.role = "up", "support"
            elif z.role == "support" and close < z.lo - b:
                broke, z.role = "down", "resistance"
            if broke:
                z.flips += 1
                z.broken_at = t
                z.breaks.append((t, broke))
                z.in_visit = z.visit_deep = False
                events.append((z.id, f"break_{broke}"))
            else:
                self._visit(z, high, low)
            if self._should_delete(z):
                self.deleted_seeds |= z.seeds
                events.append((z.id, "deleted"))
                continue
            keep.append(z)
        self.zones = keep
        return events

    @staticmethod
    def _visit(z: Zone, high: float, low: float) -> None:
        width = max(z.hi - z.lo, 1e-12)
        touching = low <= z.hi and high >= z.lo
        if touching:
            if z.role == "support":
                deep = (z.hi - low) / width > DEEP_FRACTION
            else:
                deep = (high - z.lo) / width > DEEP_FRACTION
            if deep and not z.visit_deep:
                z.deep_retests += 1
                z.visit_deep = True
            z.in_visit = True
        elif z.in_visit:
            # Left the zone without breaking it: price respected it.
            z.extra_touches += 1
            z.in_visit = z.visit_deep = False

    @staticmethod
    def _should_delete(z: Zone) -> bool:
        if z.deep_retests >= MAX_DEEP_RETESTS:
            return True
        if len(z.breaks) >= 2:
            (t1, d1), (t2, d2) = z.breaks[-2:]
            if d1 != d2 and t2 - t1 <= OPPOSITE_BREAK_WINDOW_MS:
                return True
        return False

    def get(self, zone_id: str) -> Zone | None:
        return next((z for z in self.zones if z.id == zone_id), None)
