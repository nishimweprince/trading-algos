"""Mid moves after each signal: does the signal predict price, before any fill?

Trade records mark the mid 1/5/30 s after a *fill*, so an unfilled signal says
nothing and a filled one is already adverse-selected. Here every signal counts,
measured from the signal's own time: the signed mid move (bp, positive = the
signal's direction) at each horizon, from the recorded grid mids.

The bar a signal must clear before execution matters: its mean move at the
trade's horizon at least covers the round trip, ``maker + taker`` with a market
exit (``2 x maker`` with maker exits).

Signals come from one of three places:

- ``--signals DIR``: the bridge's signal log (``<state_dir>/signals`` or
  ``<state_dir>/shadow/signals``), every action (enters and skips);
- ``--rules VERSION``: a rule model replayed over the feature rows;
- ``--candidate DIR --threshold P``: a candidate's out-of-fold scores.

A held signal fires on consecutive grid samples; only the first of an episode
(same symbol and side within ``--episode-s``) counts, so moves are not counted
many times over. Moves at long horizons still overlap across episodes, so read
the t-stat as optimistic::

    python -m research.markouts --features research/data/features/dev \\
        --days 2026-10-08 2026-10-09 --signals data/state/dev/shadow/signals \\
        --out research/data/markouts/shadow-rules-v5
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ofi_scalper_service.trades import SIGNAL_HORIZONS_S

from .dataset import GRID_NS, load_days

__all__ = [
    "MARKOUT_HORIZONS_S",
    "candidate_signals",
    "episodes",
    "forward_moves",
    "load_signal_log",
    "markouts",
    "rule_signals",
    "summarise_moves",
]

MARKOUT_HORIZONS_S = SIGNAL_HORIZONS_S  # the live bridge's markouts, for comparison
NS_PER_S = 1_000_000_000


def _key(horizon_s: float) -> str:
    return f"{horizon_s:g}s"


# --- signals -----------------------------------------------------------------------------


def load_signal_log(directory: Path, days: Iterable[str]) -> pd.DataFrame:
    """``symbol, t_ns, side, action`` of each day's signal records that have a side."""
    rows: list[dict[str, Any]] = []
    for day in days:
        path = directory / f"{day}.jsonl"
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("side") in ("buy", "sell") and "t_ns" in record:
                rows.append(
                    {
                        "symbol": record["symbol"],
                        "t_ns": int(record["t_ns"]),
                        "side": record["side"],
                        "action": str(record.get("action", "")).split(":")[0],
                    }
                )
    return pd.DataFrame(rows, columns=["symbol", "t_ns", "side", "action"])


def rule_signals(rows: pd.DataFrame, rules: dict[str, Any]) -> pd.DataFrame:
    """Replay a rule scorer over grid rows; a signal wherever it starts holding a side."""
    from ofi_scalper_service.rules import RuleParams, RuleScorer

    scorer = RuleScorer(RuleParams(**rules))
    needed = ["symbol", "t_ns", *scorer.features]
    out: list[dict[str, Any]] = []
    held: dict[str, str | None] = {}
    for record in rows[needed].to_dict("records"):
        scores = scorer.score(record)
        side = None
        if scores is not None and scores.up > 0:
            side = "buy"
        elif scores is not None and scores.down > 0:
            side = "sell"
        symbol = record["symbol"]
        if side is not None and held.get(symbol) != side:
            out.append({"symbol": symbol, "t_ns": int(record["t_ns"]), "side": side})
        held[symbol] = side
    frame = pd.DataFrame(out, columns=["symbol", "t_ns", "side"])
    frame["action"] = "rule"
    return frame


def candidate_signals(oof: pd.DataFrame, threshold: float) -> pd.DataFrame:
    """Out-of-fold rows whose larger directional probability reaches ``threshold``."""
    up, down = oof["up"].to_numpy(), oof["down"].to_numpy()
    p = np.maximum(up, down)
    keep = p >= threshold
    frame = pd.DataFrame(
        {
            "symbol": oof["symbol"].astype(str).to_numpy()[keep],
            "t_ns": oof["t_ns"].to_numpy()[keep].astype(np.int64),
            "side": np.where(up >= down, "buy", "sell")[keep],
        }
    )
    frame["action"] = "candidate"
    return frame


def episodes(signals: pd.DataFrame, episode_s: float) -> pd.DataFrame:
    """The first signal of each run of the same symbol and side within ``episode_s``."""
    if signals.empty:
        return signals
    ordered = signals.sort_values(["symbol", "side", "t_ns"], kind="stable")
    gap_ns = int(episode_s * NS_PER_S)
    same = (ordered["symbol"] == ordered["symbol"].shift()) & (
        ordered["side"] == ordered["side"].shift()
    )
    starts = ~same | (ordered["t_ns"].diff() > gap_ns)
    return ordered[starts].sort_values("t_ns", kind="stable").reset_index(drop=True)


# --- moves -------------------------------------------------------------------------------


def forward_moves(
    t_ns: np.ndarray,
    mid: np.ndarray,
    at_ns: np.ndarray,
    sign: np.ndarray,
    horizons_s: Iterable[float] = MARKOUT_HORIZONS_S,
    *,
    grid_ns: int = GRID_NS,
) -> dict[str, np.ndarray]:
    """Signed mid moves (bp) from each ``at_ns`` for one symbol's grid series.

    The move starts at the first grid sample at or after the signal (none within
    two grid steps: NaN) and ends at the first sample at or after ``+ horizon``.
    A recording gap in between (consecutive samples more than two grid steps
    apart, as ``dataset.triple_barrier``), a missing mid or the end of data: NaN.
    """
    t_ns = np.asarray(t_ns, dtype=np.int64)
    mid = np.asarray(mid, dtype=float)
    at_ns = np.asarray(at_ns, dtype=np.int64)
    sign = np.asarray(sign, dtype=float)
    n = len(t_ns)
    gaps = np.zeros(n, dtype=np.int64)
    if n > 1:
        gaps[1:] = np.cumsum(np.diff(t_ns) > 2 * grid_ns)
    start = np.searchsorted(t_ns, at_ns, side="left")
    ok = start < n
    start_c = np.minimum(start, max(n - 1, 0))
    if n:
        ok &= t_ns[start_c] - at_ns <= 2 * grid_ns
        base = mid[start_c]
    else:
        base = np.full(len(at_ns), np.nan)
    out: dict[str, np.ndarray] = {}
    for h in horizons_s:
        moves = np.full(len(at_ns), np.nan)
        if n:
            end = np.searchsorted(t_ns, t_ns[start_c] + int(round(h * NS_PER_S)), side="left")
            valid = ok & (end < n)
            end_c = np.minimum(end, n - 1)
            valid &= gaps[end_c] == gaps[start_c]
            with np.errstate(invalid="ignore", divide="ignore"):
                raw = sign * (mid[end_c] / base - 1.0) * 10_000
            valid &= np.isfinite(raw)
            moves[valid] = raw[valid]
        out[_key(h)] = moves
    return out


def markouts(
    rows: pd.DataFrame, signals: pd.DataFrame, horizons_s: Iterable[float] = MARKOUT_HORIZONS_S
) -> pd.DataFrame:
    """``signals`` with one move column per horizon, from the grid rows' mids."""
    horizons_s = tuple(horizons_s)
    parts = []
    for symbol, group in signals.groupby("symbol", sort=False, observed=True):
        series = rows[rows["symbol"].astype(str) == str(symbol)]
        sign = np.where(group["side"].to_numpy() == "buy", 1.0, -1.0)
        moves = forward_moves(
            series["t_ns"].to_numpy(),
            series["mid"].to_numpy(),
            group["t_ns"].to_numpy(),
            sign,
            horizons_s,
        )
        parts.append(group.reset_index(drop=True).assign(**moves))
    if not parts:
        return signals.assign(**{_key(h): pd.Series(dtype=float) for h in horizons_s})
    return pd.concat(parts, ignore_index=True)


@dataclass(frozen=True, slots=True)
class Costs:
    maker_bp: float = 2.0
    taker_bp: float = 5.0

    @property
    def market_exit_bp(self) -> float:
        return self.maker_bp + self.taker_bp

    @property
    def maker_exit_bp(self) -> float:
        return 2 * self.maker_bp


def _stats(values: np.ndarray, costs: Costs) -> dict[str, Any]:
    values = values[np.isfinite(values)]
    n = len(values)
    if n == 0:
        return {"n": 0}
    mean = float(values.mean())
    sd = float(values.std(ddof=1)) if n > 1 else 0.0
    return {
        "n": n,
        "mean_bp": round(mean, 3),
        "median_bp": round(float(np.median(values)), 3),
        "t_stat": round(mean / (sd / math.sqrt(n)), 3) if sd > 0 else None,
        "above_market_exit_cost": round(float((values > costs.market_exit_bp).mean()), 4),
        "above_maker_exit_cost": round(float((values > costs.maker_exit_bp).mean()), 4),
    }


def summarise_moves(
    frame: pd.DataFrame,
    horizons_s: Iterable[float] = MARKOUT_HORIZONS_S,
    costs: Costs | None = None,
) -> dict[str, Any]:
    """Per horizon: overall, by symbol, by side and by action."""
    horizons_s = tuple(horizons_s)
    costs = costs or Costs()

    def block(sub: pd.DataFrame) -> dict[str, Any]:
        return {_key(h): _stats(sub[_key(h)].to_numpy(dtype=float), costs) for h in horizons_s}

    out: dict[str, Any] = {
        "signals": int(len(frame)),
        "costs_bp": {"market_exit": costs.market_exit_bp, "maker_exit": costs.maker_exit_bp},
        "overall": block(frame),
    }
    for column in ("symbol", "side", "action"):
        if column in frame.columns:
            out[f"by_{column}"] = {
                str(k): block(g) for k, g in frame.groupby(column, sort=True, observed=True)
            }
    return out


def report_markdown(summary: dict[str, Any], title: str) -> str:
    costs = summary["costs_bp"]
    lines = [
        f"# Markouts from signal: {title}",
        "",
        f"{summary['signals']} signal episodes. Signed mid move in bp (positive = the "
        f"signal's direction). Round trip: {costs['market_exit']} bp with a market exit, "
        f"{costs['maker_exit']} bp with a maker exit. Overlapping horizons make the "
        "t-stat optimistic.",
        "",
        "| horizon | n | mean bp | median bp | t | > market-exit cost | > maker-exit cost |",
        "|---|---|---|---|---|---|---|",
    ]
    for key, s in summary["overall"].items():
        if not s.get("n"):
            lines.append(f"| {key} | 0 | | | | | |")
            continue
        lines.append(
            f"| {key} | {s['n']} | {s['mean_bp']} | {s['median_bp']} | {s['t_stat']} "
            f"| {s['above_market_exit_cost']} | {s['above_maker_exit_cost']} |"
        )
    return "\n".join(lines) + "\n"


# --- CLI ---------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="research.markouts", description=__doc__.split("\n")[0])
    parser.add_argument("--features", type=Path, required=True, help="features/<profile> dir")
    parser.add_argument("--days", nargs="+", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--signals", type=Path, help="a bridge's signals/ directory")
    source.add_argument("--rules", help="a rule model version under --model-dir")
    source.add_argument("--candidate", type=Path, help="a candidate dir (out-of-fold scores)")
    parser.add_argument("--model-dir", type=Path, default=Path("models"))
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--episode-s", type=float, default=5.0)
    parser.add_argument("--maker-bp", type=float, default=2.0)
    parser.add_argument("--taker-bp", type=float, default=5.0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    rows = load_days(args.features, args.days)
    if args.signals is not None:
        signals, title = load_signal_log(args.signals, args.days), str(args.signals)
    elif args.rules is not None:
        rules = json.loads((args.model_dir / args.rules / "rules.json").read_text())
        signals, title = rule_signals(rows, rules), args.rules
    else:
        from .scores import load_table
        from .train import Candidate

        candidate = Candidate.load(args.candidate)
        oof = load_table([candidate.oof_dir() / f"{day}.parquet" for day in args.days])
        signals = candidate_signals(oof, args.threshold)
        title = f"{candidate.version} p>={args.threshold:g}"
    frame = markouts(rows, episodes(signals, args.episode_s))
    summary = summarise_moves(frame, costs=Costs(args.maker_bp, args.taker_bp))
    summary["source"], summary["days"] = title, list(args.days)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "markouts.json").write_text(json.dumps(summary, indent=2))
    (args.out / "MARKOUTS.md").write_text(report_markdown(summary, title))
    print(report_markdown(summary, title))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
