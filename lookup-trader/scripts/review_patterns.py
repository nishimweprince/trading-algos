#!/usr/bin/env python3
"""Render detected chart patterns to a standalone HTML page.

The detectors decide what lands in `bar_tags`, and from there what a base rate is
conditioned on, so their output has to be looked at rather than only counted. A
6% hit rate means nothing if the 6% are not formations.

Emits one SVG per detection — candles, the pivots the match used, the anchor bar
— grouped by setup, so a whole family can be judged at once.
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.config import settings  # noqa: E402
from app.db.duck import register_candles_view  # noqa: E402
from app.taggers.chart.patterns import Detection, detect  # noqa: E402
from app.taggers.chart.swings import pivots  # noqa: E402

WINDOW = 180
PAD = 8


@dataclass(frozen=True)
class ReviewItem:
    """One auditable detection; `verdict` is filled by a human downstream."""

    detection_id: str
    setup_id: str
    anchor_ts: str
    state: str
    confidence: float
    pivot_indices: tuple[int, ...]
    card_html: str

    def export(self) -> dict:
        return {
            "detection_id": self.detection_id,
            "setup_id": self.setup_id,
            "anchor_ts": self.anchor_ts,
            "state": self.state,
            "confidence": self.confidence,
            "pivot_indices": list(self.pivot_indices),
            # Reviewers set one of accept/reject/unsure. Null means unreviewed.
            "verdict": None,
            "notes": "",
        }


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


def _svg(window: pd.DataFrame, det: Detection, width: int = 420, height: int = 210) -> str:
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

    pts = [(x(p.index), y(p.price)) for p in det.pivots]
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


def collect(bars: pd.DataFrame, per_setup: int, context: int) -> dict[str, list[ReviewItem]]:
    """Scan for formations, keeping the first `per_setup` distinct ones per setup."""
    out: dict[str, list[ReviewItem]] = defaultdict(list)
    seen: set[tuple] = set()

    for i in range(WINDOW, len(bars)):
        atr = float(bars["atr_at_bar"].iloc[i] or 0.0)
        if atr <= 0:
            continue
        window = bars.iloc[i - WINDOW + 1 : i + 1]
        found = detect(window, pivots(window, settings.swing_lookback), atr)

        for det in found:
            setup = det.tag.setup_id
            if len(out[setup]) >= per_setup:
                continue
            # Identify a formation by its pivot bars so overlapping anchors on
            # the same structure are not shown several times over.
            absolute_pivots = tuple(i - WINDOW + 1 + p.index for p in det.pivots)
            key = (setup, absolute_pivots)
            if key in seen:
                continue
            seen.add(key)

            start = max(min(p.index for p in det.pivots) - context, 0)
            shifted = Detection(
                det.tag, tuple(p._replace(index=p.index - start) for p in det.pivots)
            )
            svg = _svg(window.iloc[start:], shifted)
            anchor_ts = pd.Timestamp(bars["ts"].iloc[i]).isoformat()
            stamp = pd.Timestamp(anchor_ts).strftime("%Y-%m-%d %H:%M")
            state = det.tag.state
            out[setup].append(
                ReviewItem(
                    detection_id=f"{setup}:{anchor_ts}",
                    setup_id=setup,
                    anchor_ts=anchor_ts,
                    state=state,
                    confidence=det.tag.confidence,
                    pivot_indices=absolute_pivots,
                    card_html=(
                        f"<figure><figcaption><span>{html.escape(stamp)}</span>"
                        f'<span class="{state}">{state} · {det.tag.confidence:.2f}</span>'
                        f"</figcaption>{svg}</figure>"
                    ),
                )
            )
    return out


def render(groups: dict[str, list[ReviewItem]], symbol: str, timeframe: str) -> str:
    sections = []
    for setup in sorted(groups):
        cards = "".join(item.card_html for item in groups[setup])
        sections.append(
            f"<h2>{html.escape(setup)} <em>{len(groups[setup])}</em></h2><div>{cards}</div>"
        )
    body = "".join(sections) or "<p>Nothing detected.</p>"
    return f"""<!doctype html>
<meta charset="utf-8">
<title>Chart patterns — {html.escape(symbol)} {html.escape(timeframe)}</title>
<style>
  body {{ background:#000; color:#e5e7eb;
          font:14px/1.5 ui-sans-serif,system-ui,sans-serif; margin:24px; }}
  h1 {{ font-size:18px; font-weight:600; }}
  h1 span {{ color:#9ca3af; font-weight:400; }}
  h2 {{ font-size:14px; font-weight:600; margin:28px 0 10px; }}
  h2 em {{ color:#6b7280; font-style:normal; font-weight:400; }}
  div {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:12px; }}
  figure {{ margin:0; border:1px solid rgba(255,255,255,.15); background:#000; padding:8px; }}
  figcaption {{ display:flex; justify-content:space-between; font-size:12px;
                color:#9ca3af; margin-bottom:6px; }}
  .complete {{ color:#38bdf8; }}
  .forming {{ color:#6b7280; }}
  p.key {{ color:#6b7280; font-size:12px; max-width:70ch; }}
</style>
<h1>Chart patterns <span>{html.escape(symbol)} {html.escape(timeframe)}</span></h1>
<p class="key">Blue = the pivots the match used. Amber = the anchor bar. The label on
each card is the tag state and its match-quality score: <strong>forming</strong> means
the shape is there but price has not left it yet, <strong>complete</strong> means the
neckline or boundary broke. Only complete tags should reach a base rate.</p>
{body}
"""


def export_review(
    groups: dict[str, list[ReviewItem]], symbol: str, timeframe: str
) -> dict:
    """Stable JSON contract for review tools and aggregate QA."""
    items = [item for setup in sorted(groups) for item in groups[setup]]
    states = Counter(item.state for item in items)
    return {
        "schema_version": "1.0",
        "symbol": symbol,
        "timeframe": timeframe,
        "verdict_values": ["accept", "reject", "unsure"],
        "summary": {
            "detections": len(items),
            "by_setup": {setup: len(groups[setup]) for setup in sorted(groups)},
            "by_state": dict(sorted(states.items())),
            "mean_confidence": (
                round(sum(item.confidence for item in items) / len(items), 4) if items else None
            ),
            "reviewed": 0,
            "accepted": 0,
            "rejected": 0,
            "unsure": 0,
        },
        "items": [item.export() for item in items],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--timeframe", default="H1")
    ap.add_argument("--per-setup", type=int, default=6)
    ap.add_argument("--context", type=int, default=25, help="Bars to show before the first pivot.")
    ap.add_argument("--out", type=Path, default=Path("patterns.html"))
    ap.add_argument(
        "--json-out",
        type=Path,
        help="Machine-readable review file (default: HTML output with .json suffix).",
    )
    args = ap.parse_args()

    bars = _load(args.symbol, args.timeframe)
    print(f"{args.symbol} {args.timeframe}: {len(bars)} bars with ATR")
    groups = collect(bars, args.per_setup, args.context)
    args.out.write_text(render(groups, args.symbol, args.timeframe))
    json_out = args.json_out or args.out.with_suffix(".json")
    json_out.write_text(
        json.dumps(export_review(groups, args.symbol, args.timeframe), indent=2) + "\n"
    )

    for setup in sorted(groups):
        print(f"  {setup:24} {len(groups[setup])}")
    print(f"Wrote {args.out}")
    print(f"Wrote {json_out}")


if __name__ == "__main__":
    main()
