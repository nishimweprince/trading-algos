#!/usr/bin/env python3
"""Render detected chart-pattern candidates to a standalone HTML page.

The candidate filter decides where the labelling budget goes and what the
classifier's training set looks like, so its output has to be looked at rather
than only counted. A 6% hit rate is meaningless if the 6% are not formations.

Emits one SVG per example — candles, the pivots the match used, and the anchor
bar — grouped by setup so a whole family can be judged at once.
"""

from __future__ import annotations

import argparse
import glob
import html
import sys
from collections import defaultdict
from pathlib import Path

import duckdb
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.config import settings  # noqa: E402
from app.db.duck import register_candles_view  # noqa: E402
from app.taggers.chart.candidates import Candidate, candidates  # noqa: E402
from app.taggers.chart.swings import pivots  # noqa: E402

WINDOW = 180
PAD = 8


def _load(symbol: str, timeframe: str) -> pd.DataFrame:
    con = duckdb.connect(":memory:")
    register_candles_view(con, force=True)
    try:
        bars = con.execute(
            "SELECT ts, open, high, low, close FROM candles "
            "WHERE symbol = ? AND timeframe = ? ORDER BY ts",
            [symbol, timeframe],
        ).df()
    finally:
        con.close()

    parts = glob.glob(
        str(_REPO_ROOT / f"data/features/symbol={symbol}/timeframe={timeframe}/**/part-*.parquet"),
        recursive=True,
    )
    if not parts:
        raise SystemExit(f"No feature store for {symbol} {timeframe} — build it first.")
    atr = pd.concat([pd.read_parquet(p, columns=["ts", "atr_at_bar"]) for p in parts])

    bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
    atr["ts"] = pd.to_datetime(atr["ts"], utc=True)
    return bars.merge(atr, on="ts", how="inner").sort_values("ts").reset_index(drop=True)


def _svg(window: pd.DataFrame, cand: Candidate, width: int = 420, height: int = 210) -> str:
    """One formation as a candlestick SVG, pivots and anchor marked."""
    lo, hi = float(window["low"].min()), float(window["high"].max())
    span = (hi - lo) or 1e-9
    n = len(window)
    step = (width - 2 * PAD) / n

    def x(i: int) -> float:
        return PAD + (i + 0.5) * step

    def y(price: float) -> float:
        return PAD + (hi - price) / span * (height - 2 * PAD)

    parts: list[str] = []
    body_w = max(step * 0.6, 1.0)
    for i, row in enumerate(window.itertuples()):
        up = row.close >= row.open
        colour = "#4b5563" if up else "#374151"
        parts.append(
            f'<line x1="{x(i):.1f}" y1="{y(row.high):.1f}" x2="{x(i):.1f}" '
            f'y2="{y(row.low):.1f}" stroke="{colour}" stroke-width="1"/>'
        )
        top, bottom = (row.close, row.open) if up else (row.open, row.close)
        parts.append(
            f'<rect x="{x(i) - body_w / 2:.1f}" y="{y(top):.1f}" width="{body_w:.1f}" '
            f'height="{max(y(bottom) - y(top), 0.8):.1f}" fill="{colour}"/>'
        )

    # The pivots the match was built from, connected in order.
    pts = [(x(p.index), y(p.price)) for p in cand.pivots_used]
    if len(pts) > 1:
        path = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
        parts.append(
            f'<polyline points="{path}" fill="none" stroke="#38bdf8" '
            f'stroke-width="1.5" stroke-dasharray="4 3"/>'
        )
    for px, py in pts:
        parts.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3.5" fill="#38bdf8"/>')

    anchor = x(n - 1)
    parts.append(
        f'<line x1="{anchor:.1f}" y1="{PAD}" x2="{anchor:.1f}" y2="{height - PAD}" '
        f'stroke="#f59e0b" stroke-width="1" stroke-dasharray="2 2"/>'
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'preserveAspectRatio="xMidYMid meet">{"".join(parts)}</svg>'
    )


def collect(bars: pd.DataFrame, per_setup: int, context: int) -> dict[str, list[str]]:
    """Scan for formations, keeping the first `per_setup` distinct ones per setup."""
    out: dict[str, list[str]] = defaultdict(list)
    seen: set[tuple] = set()

    for i in range(WINDOW, len(bars)):
        atr = float(bars["atr_at_bar"].iloc[i] or 0.0)
        if atr <= 0:
            continue
        window = bars.iloc[i - WINDOW + 1 : i + 1]
        cand = candidates(
            pivots(window, settings.swing_lookback), anchor_index=len(window) - 1, atr=atr
        )
        if cand is None:
            continue

        # Identify a formation by its pivot bars so overlapping anchors on the
        # same structure are not shown several times over.
        key = (cand.setup_ids, tuple(i - WINDOW + 1 + p.index for p in cand.pivots_used))
        if key in seen:
            continue
        seen.add(key)

        if all(len(out[s]) >= per_setup for s in cand.setup_ids):
            continue

        first = min(p.index for p in cand.pivots_used)
        start = max(first - context, 0)
        trimmed = window.iloc[start:]
        shifted = Candidate(
            cand.setup_ids,
            tuple(p._replace(index=p.index - start) for p in cand.pivots_used),
        )
        svg = _svg(trimmed, shifted)
        stamp = pd.Timestamp(bars["ts"].iloc[i]).strftime("%Y-%m-%d %H:%M")
        card = (
            f'<figure><figcaption>{html.escape(stamp)} UTC'
            f'<span>{html.escape(", ".join(cand.setup_ids))}</span></figcaption>{svg}</figure>'
        )
        for s in cand.setup_ids:
            if len(out[s]) < per_setup:
                out[s].append(card)
    return out


def render(groups: dict[str, list[str]], symbol: str, timeframe: str) -> str:
    sections = []
    for setup in sorted(groups):
        cards = "".join(groups[setup])
        sections.append(f"<h2>{html.escape(setup)} <em>{len(groups[setup])}</em></h2><div>{cards}</div>")
    body = "".join(sections) or "<p>No candidates found.</p>"
    return f"""<!doctype html>
<meta charset="utf-8">
<title>Chart-pattern candidates — {html.escape(symbol)} {html.escape(timeframe)}</title>
<style>
  body {{ background:#000; color:#e5e7eb; font:14px/1.5 ui-sans-serif,system-ui,sans-serif; margin:24px; }}
  h1 {{ font-size:18px; font-weight:600; }}
  h1 span {{ color:#9ca3af; font-weight:400; }}
  h2 {{ font-size:14px; font-weight:600; margin:28px 0 10px; color:#e5e7eb; }}
  h2 em {{ color:#6b7280; font-style:normal; font-weight:400; }}
  div {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:12px; }}
  figure {{ margin:0; border:1px solid rgba(255,255,255,.15); background:#000; padding:8px; }}
  figcaption {{ display:flex; justify-content:space-between; font-size:12px;
                color:#9ca3af; margin-bottom:6px; }}
  figcaption span {{ color:#38bdf8; }}
  p.key {{ color:#6b7280; font-size:12px; }}
</style>
<h1>Chart-pattern candidates <span>{html.escape(symbol)} {html.escape(timeframe)}</span></h1>
<p class="key">Blue = the pivots the match used. Amber = the anchor bar the pattern was
detected at. These are <strong>candidates</strong>, not verdicts — the filter is tuned
for recall, so the question is whether real formations are being missed, not whether
every card is textbook.</p>
{body}
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--timeframe", default="H1")
    ap.add_argument("--per-setup", type=int, default=6)
    ap.add_argument("--context", type=int, default=25, help="Bars to show before the first pivot.")
    ap.add_argument("--out", type=Path, default=Path("candidates.html"))
    args = ap.parse_args()

    bars = _load(args.symbol, args.timeframe)
    print(f"{args.symbol} {args.timeframe}: {len(bars)} bars with ATR")
    groups = collect(bars, args.per_setup, args.context)
    args.out.write_text(render(groups, args.symbol, args.timeframe))

    for setup in sorted(groups):
        print(f"  {setup:24} {len(groups[setup])}")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
