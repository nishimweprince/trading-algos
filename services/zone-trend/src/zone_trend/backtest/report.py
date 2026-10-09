"""Write a run to ``runs/{run_id}/`` and render its self-contained report."""

from __future__ import annotations

import html
import json
import math
from pathlib import Path

import pandas as pd
import yaml

from zone_trend.backtest.engine import RunResult
from zone_trend.backtest.metrics import summary
from zone_trend.config import Config

__all__ = ["write_run"]


def _fmt(v) -> str:
    if isinstance(v, float):
        if math.isnan(v):
            return "–"
        return f"{v:,.4f}" if abs(v) < 10 else f"{v:,.2f}"
    return html.escape(str(v))


def _table(rows: list[dict]) -> str:
    if not rows:
        return "<p>none</p>"
    cols = list(rows[0])
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    body = "".join(
        "<tr>" + "".join(f"<td>{_fmt(r.get(c))}</td>" for c in cols) + "</tr>" for r in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _equity_svg(equity: pd.DataFrame, w: int = 900, h: int = 260) -> str:
    if len(equity) < 2:
        return ""
    step = max(1, len(equity) // 1500)
    e = equity.iloc[::step]
    x0, x1 = e["time"].iloc[0], e["time"].iloc[-1]
    y0, y1 = e["equity"].min(), e["equity"].max()
    span_y = (y1 - y0) or 1.0
    pts = " ".join(
        f"{(t - x0) / (x1 - x0) * w:.1f},{h - (v - y0) / span_y * h:.1f}"
        for t, v in zip(e["time"], e["equity"], strict=True)
    )
    return (
        f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="equity curve">'
        f'<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="{pts}"/>'
        f"</svg><p class=muted>equity {y0:,.2f} – {y1:,.2f}</p>"
    )


_CSS = """
:root { --fg:#1d1d1f; --bg:#fff; --muted:#666; --line:#ddd; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e8e8e8; --bg:#161616; --muted:#999; --line:#333; }
}
body { font: 14px/1.45 system-ui, sans-serif; color: var(--fg); background: var(--bg);
       max-width: 1000px; margin: 2rem auto; padding: 0 16px; }
table { border-collapse: collapse; margin: .5rem 0 1.5rem; font-variant-numeric: tabular-nums; }
td, th { border-bottom: 1px solid var(--line); padding: 3px 10px; text-align: right; }
th:first-child, td:first-child { text-align: left; }
.muted { color: var(--muted); }
"""


def render(result: RunResult, stats: dict) -> str:
    meta = result.meta
    head = {k: v for k, v in stats.items() if not isinstance(v, (list, dict))}
    reasons = []
    if not result.signals.empty:
        rej = result.signals[result.signals["decision"] != "accepted"]
        counts = rej.groupby(["stage", "reason"]).size().reset_index(name="count")
        reasons = counts.sort_values("count", ascending=False).to_dict("records")
    exits = [{"exit_reason": k, "trades": v} for k, v in stats.get("exit_reasons", {}).items()]
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backtest {html.escape(result.run_id)}</title><style>{_CSS}</style></head><body>
<h1>Backtest {html.escape(result.run_id)}</h1>
<p class=muted>{html.escape(meta["kind"])} · {html.escape(" → ".join(meta["range"]))} ·
snapshot {html.escape(meta["snapshot_id"])} · exchangeInfo {html.escape(str(meta["exchange_info"]))}
· {len(meta["symbols"])} symbols</p>
{_equity_svg(result.equity)}
<h2>Summary</h2>{_table([head])}
<h2>Per symbol</h2>{_table(stats.get("per_symbol", []))}
<h2>Exit reasons</h2>{_table(exits)}
<h2>Rejected signals</h2>{_table(reasons)}
<h2>Run notes</h2>
<p>Same-minute stop/TP conflicts: {meta["same_minute_conflicts"]} · mark minutes filled from
last price: {meta["mark_minutes_missing"]} · halted: {meta["halted"]} · breaker events:
{len(meta["breaker_events"])}</p>
<ul>{"".join(f"<li>{html.escape(a)}</li>" for a in meta["approximations"])}</ul>
</body></html>"""


def write_run(result: RunResult, cfg: Config, runs_dir: Path) -> Path:
    out = runs_dir / result.run_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "config.yaml").write_text(yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False))
    (out / "snapshot_id").write_text(result.meta["snapshot_id"] + "\n")
    for name in ("trades", "fills", "equity", "signals"):
        getattr(result, name).to_parquet(out / f"{name}.parquet", index=False)
    stats = summary(result.trades, result.equity)
    (out / "meta.json").write_text(
        json.dumps({**result.meta, "summary": stats}, indent=1, default=str)
    )
    (out / "report.html").write_text(render(result, stats))
    return out
