"""Replay parity vs the research reference trades (SPEC §8.2).

Pass criteria per strategy:
  * entry times: >= 99% of reference entries reproduced (and >= 99% of ours are in the reference);
  * per-trade R_net within +-0.01 where entries match;
  * trade count and mean R within 2% of the reference (mean R: 2% relative, or 0.005R absolute when tiny).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .config import Config


@dataclass
class Row:
    sid: str
    n_ref: int
    n_ours: int
    matched: int
    r_bad: int
    mean_ref: float
    mean_ours: float

    @property
    def recall(self) -> float:
        return self.matched / self.n_ref if self.n_ref else 1.0

    @property
    def precision(self) -> float:
        return self.matched / self.n_ours if self.n_ours else 1.0

    @property
    def ok(self) -> bool:
        cnt_ok = abs(self.n_ours - self.n_ref) <= 0.02 * self.n_ref
        mean_ok = abs(self.mean_ours - self.mean_ref) <= max(0.02 * abs(self.mean_ref), 0.005)
        return self.recall >= 0.99 and self.precision >= 0.99 and self.r_bad == 0 and cnt_ok and mean_ok


def _read(path: Path) -> dict[str, float]:
    out = {}
    with path.open() as f:
        for r in csv.DictReader(f):
            out[r["entry_time_est"][:16]] = float(r["R_net"])
    return out


def compare(ours: Path, ref: Path, sid: str) -> Row:
    a = _read(ours / f"{sid}_trades.csv")
    b = _read(ref / f"{sid}_trades.csv")
    common = a.keys() & b.keys()
    bad = sum(1 for k in common if abs(a[k] - b[k]) > 0.01)
    ma = sum(a.values()) / len(a) if a else 0.0
    mb = sum(b.values()) / len(b) if b else 0.0
    return Row(sid, len(b), len(a), len(common), bad, mb, ma)


def report(ours: Path, ref: Path, cfg: Config, ids: list[str] | None = None) -> bool:
    print(f"{'id':4} {'ref':>5} {'ours':>5} {'recall':>7} {'prec':>7} {'Rbad':>5} {'meanR ref':>9} {'ours':>7}  ok")
    all_ok = True
    for s in cfg.strategies:
        if ids and s.id not in ids:
            continue
        if not (ours / f"{s.id}_trades.csv").exists():
            continue
        r = compare(ours, ref, s.id)
        all_ok &= r.ok
        print(f"{r.sid:4} {r.n_ref:5d} {r.n_ours:5d} {r.recall:7.3f} {r.precision:7.3f} {r.r_bad:5d} "
              f"{r.mean_ref:+9.3f} {r.mean_ours:+7.3f}  {'PASS' if r.ok else 'FAIL'}")
    return all_ok
