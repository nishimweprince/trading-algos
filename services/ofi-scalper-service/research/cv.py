"""Periods, purged walk-forward folds, and the held-out period that is touched once.

- **Periods** are ISO weeks (provisional runs) or calendar months (binding).
- **Folds** expand forward: fold k trains on the periods before k and is
  validated on period k. The held-out (last) period is in no fold.
- **Purge and embargo**: a training row whose label window ``[t, t + h]``,
  plus the embargo, reaches the validation period is dropped.
- **Splits** are written once per run name and never change, so the held-out
  period cannot drift as data arrives. **The ledger** records each evaluation
  of a held-out period; a second one is refused (no tuning on the holdout).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

__all__ = [
    "EMBARGO_NS",
    "HoldoutUsed",
    "Split",
    "load_or_create_split",
    "period_of",
    "purge",
    "record_holdout_use",
]

EMBARGO_NS = 5 * 60 * 1_000_000_000


class HoldoutUsed(RuntimeError):
    """The held-out period of this run was already evaluated."""


def period_of(day: str, period: str) -> str:
    d = date.fromisoformat(day)
    if period == "week":
        year, week, _ = d.isocalendar()
        return f"{year}-W{week:02d}"
    if period == "month":
        return d.strftime("%Y-%m")
    raise ValueError(f"period must be week or month, not {period!r}")


@dataclass(frozen=True)
class Split:
    run: str
    period: str
    walk_forward: tuple[str, ...]
    holdout: tuple[str, ...]
    days: dict[str, list[str]]  # period -> eligible days in it
    binding: bool
    created_at: str

    @property
    def folds(self) -> list[tuple[tuple[str, ...], str]]:
        return [
            (self.walk_forward[:k], self.walk_forward[k]) for k in range(1, len(self.walk_forward))
        ]

    def days_of(self, periods: tuple[str, ...] | list[str]) -> list[str]:
        return sorted(day for p in periods for day in self.days.get(p, []))

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["walk_forward"], out["holdout"] = list(self.walk_forward), list(self.holdout)
        return out


def load_or_create_split(
    splits_dir: Path,
    run: str,
    *,
    days: list[str],
    period: str,
    binding: bool,
    min_walk_forward: int = 3,
) -> Split:
    """The run's split: created from ``days`` the first time, then fixed forever."""
    path = splits_dir / f"{run}.json"
    if path.is_file():
        raw = json.loads(path.read_text())
        return Split(
            run=raw["run"],
            period=raw["period"],
            walk_forward=tuple(raw["walk_forward"]),
            holdout=tuple(raw["holdout"]),
            days=raw["days"],
            binding=raw["binding"],
            created_at=raw["created_at"],
        )
    by_period: dict[str, list[str]] = {}
    for day in sorted(days):
        by_period.setdefault(period_of(day, period), []).append(day)
    periods = sorted(by_period)
    if len(periods) < min_walk_forward + 1:
        raise ValueError(
            f"{len(periods)} {period}(s) of eligible data; a run needs {min_walk_forward} "
            f"walk-forward {period}s plus 1 held-out {period}"
        )
    split = Split(
        run=run,
        period=period,
        walk_forward=tuple(periods[:-1]),
        holdout=(periods[-1],),
        days=by_period,
        binding=binding,
        created_at=datetime.now(UTC).isoformat(),
    )
    splits_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(split.as_dict(), indent=2))
    return split


def purge(
    train: pd.DataFrame, validation_start_ns: int, horizon_s: float, embargo_ns: int = EMBARGO_NS
) -> pd.DataFrame:
    """Drop training rows whose label window (plus embargo) reaches the validation start."""
    reach = train["t_ns"].to_numpy(dtype=np.int64) + int(horizon_s * 1e9) + embargo_ns
    return train[reach < validation_start_ns]


def record_holdout_use(ledger_path: Path, split: Split, candidate: str) -> None:
    """Record this evaluation; raise if the held-out period was already evaluated."""
    ledger: dict[str, Any] = json.loads(ledger_path.read_text()) if ledger_path.is_file() else {}
    key = f"{split.run}:{','.join(split.holdout)}"
    if key in ledger:
        previous = ledger[key]
        raise HoldoutUsed(
            f"held-out {split.holdout} of run {split.run!r} was already evaluated "
            f"({previous['candidate']} at {previous['at']}). Do not tune on the holdout: "
            "start a new run with new data instead."
        )
    ledger[key] = {"candidate": candidate, "at": datetime.now(UTC).isoformat()}
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(ledger, indent=2))
