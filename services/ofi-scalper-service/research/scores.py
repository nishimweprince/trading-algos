"""Batch scores, and a scorer that looks them up instead of predicting.

A backtest replays every recorded line through the live code; predicting one
row at a time inside that loop would dominate its cost. So a day's grid rows
are scored once, vectorised, with exactly the calibration the live
``model.Calibrator`` applies, and the replay's bridge reads them back by
``(symbol, t_ns)`` through ``PrecomputedScorer`` (same interface as
``model.Scorer``). A test holds the two to the same numbers.

Two kinds of table:
- **OOF**: a validation period scored by the fold model that never saw it.
  Selection runs on these.
- **final**: held-out days scored by the final model. Only the gates use them.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ofi_scalper_service.model import CLASSES, Calibrator
from ofi_scalper_service.policy import Scores

__all__ = ["PrecomputedScorer", "calibrate", "load_table", "score_frame", "write_table"]


def calibrate(raw: np.ndarray, calibrator: Calibrator) -> np.ndarray:
    """``raw[:, (down, none, up)]`` -> calibrated, renormalised; as ``Calibrator.__call__``."""
    mapped = np.empty_like(raw, dtype=float)
    for j, name in enumerate(CLASSES):
        xs, ys = calibrator.knots[name]
        mapped[:, j] = np.maximum(np.interp(raw[:, j], xs, ys), 0.0)
    total = mapped.sum(axis=1)
    out = np.zeros_like(mapped)
    ok = total > 0
    out[ok] = mapped[ok] / total[ok, None]
    out[~ok] = (0.0, 1.0, 0.0)  # Scores(up=0, down=0, none=1)
    return out


def score_frame(
    df: pd.DataFrame,
    features: list[str],
    predict: Callable[[np.ndarray], np.ndarray],
    calibrator: Calibrator,
) -> pd.DataFrame:
    """Calibrated (down, none, up) for every row with all features present."""
    complete = df[features].notna().all(axis=1).to_numpy()
    rows = df[complete]
    out = pd.DataFrame({"symbol": rows["symbol"].to_numpy(), "t_ns": rows["t_ns"].to_numpy()})
    if len(rows):
        probs = calibrate(predict(rows[features].to_numpy(dtype=float)), calibrator)
    else:
        probs = np.empty((0, 3))
    for j, name in enumerate(CLASSES):
        out[name] = probs[:, j]
    return out


def write_table(table: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(path, index=False)
    return path


def load_table(paths: list[Path]) -> pd.DataFrame:
    frames = [pd.read_parquet(p) for p in paths if p.is_file()]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


class PrecomputedScorer:
    """``model.Scorer``'s interface over a score table: lookup by (symbol, t_ns)."""

    def __init__(self, table: pd.DataFrame, features: list[str] | None = None) -> None:
        self.features = features or []
        self._by_symbol: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        if len(table):
            for symbol, rows in table.sort_values("t_ns").groupby("symbol"):
                self._by_symbol[str(symbol)] = (
                    rows["t_ns"].to_numpy(dtype=np.int64),
                    rows[list(CLASSES)].to_numpy(dtype=float),
                )
        self.hits = self.misses = 0

    def score(self, sample: dict[str, Any]) -> Scores | None:
        found = self._by_symbol.get(sample["symbol"])
        if found is None:
            self.misses += 1
            return None
        times, probs = found
        i = int(np.searchsorted(times, sample["t_ns"]))
        if i >= len(times) or times[i] != sample["t_ns"]:
            self.misses += 1
            return None
        self.hits += 1
        down, none, up = probs[i]
        return Scores(up=float(up), down=float(down), none=float(none))

    def latency(self) -> None:
        return None
