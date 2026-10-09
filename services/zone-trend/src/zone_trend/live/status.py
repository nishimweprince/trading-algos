"""Read-only status page: what the bot is doing, and what the research says.

A separate process (``zt status``) that only reads the live database (SQLite
in WAL mode allows it while the bot writes) and the ``runs/`` directory, so it
can crash, restart or be exposed through a tunnel without touching trading.

Routes:

- ``/``: dashboard, refreshing every minute: mode and stage, heartbeat, equity
  curve, drawdown and breakers, open positions, recent signals with their
  decisions and reasons, closed trades, alerts.
- ``/research``: walk-forward acceptance tables and every backtest report.
- ``/runs/...``: the run directories, so report.html pages open from there.
- ``/health``: 200 while the bot's heartbeat is under 15 minutes old, else 503,
  for an external uptime monitor. ``/health/live`` is this process only.
- ``/metrics``: Prometheus text.
- ``/api/state``: the dashboard's data as JSON.
"""

from __future__ import annotations

import html
import json
import math
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from zone_trend.live.ops import HEARTBEAT_STALE_MS
from zone_trend.persistence import models as m
from zone_trend.persistence.repo import Repo

__all__ = ["create_app"]

H = 3_600_000


def _now() -> int:
    return time.time_ns() // 1_000_000


def _ts(ms: Any) -> str:
    if not ms:
        return "–"
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime((int(ms) + 1) / 1000))


def _num(v: Any, fmt: str = ",.2f") -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    if isinstance(v, int) and not isinstance(v, bool) and fmt == ",.2f":
        return f"{v:,}"  # counts, not money
    return format(v, fmt) if isinstance(v, (int, float)) else html.escape(str(v))


def _table(rows: list[dict], cols: list[tuple[str, str]], empty: str = "none yet") -> str:
    if not rows:
        return f'<p class="muted">{empty}</p>'
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label in cols)
    body = "".join(
        "<tr>"
        + "".join(
            f"<td>{r.get(key, '') if key.startswith('_') else _num(r.get(key))}</td>"
            for key, _ in cols
        )
        + "</tr>"
        for r in rows
    )
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _svg(points: list[tuple[int, float]], w: int = 900, h: int = 220) -> str:
    if len(points) < 2:
        return '<p class="muted">no equity history yet</p>'
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    sy = (y1 - y0) or 1.0
    pts = " ".join(
        f"{(x - x0) / ((x1 - x0) or 1) * w:.1f},{h - (y - y0) / sy * h:.1f}" for x, y in points
    )
    return (
        f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" class="chart" role="img" '
        f'aria-label="equity curve"><polyline points="{pts}"/></svg>'
        f'<p class="muted">{_num(y0)} – {_num(y1)} USDT, {_ts(x0)} → {_ts(x1)} UTC</p>'
    )


CSS = """
:root{--bg:#fbfbfa;--panel:#fff;--fg:#1c1c1e;--muted:#6b6b70;--line:#e4e4e2;--accent:#2f6fde;
--good:#1a7f5a;--bad:#c0392b;--warn:#b7791f}
@media (prefers-color-scheme:dark){:root{--bg:#121214;--panel:#1b1b1e;--fg:#ececee;--muted:#9a9aa1;
--line:#2c2c31;--accent:#6ea0ff;--good:#3ccf91;--bad:#ff6b5e;--warn:#f0b45a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px 48px}
header{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:baseline;margin-bottom:16px}
h1{font-size:20px;margin:0}h2{font-size:15px;margin:28px 0 8px}
nav a{color:var(--accent);margin-right:12px;text-decoration:none}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;border:1px solid var(--line);
font-size:12px}.ok{color:var(--good);border-color:var(--good)}.bad{color:var(--bad);
border-color:var(--bad)}.warn{color:var(--warn);border-color:var(--warn)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.kpi b{display:block;font-size:20px;font-variant-numeric:tabular-nums}.kpi span{color:var(--muted);
font-size:12px}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;
font-variant-numeric:tabular-nums;background:var(--panel)}th,td{padding:5px 10px;text-align:right;
border-bottom:1px solid var(--line);white-space:nowrap}th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:500;font-size:12px}.muted{color:var(--muted)}
.chart{width:100%;height:220px;background:var(--panel);border:1px solid var(--line);border-radius:10px}
.chart polyline{fill:none;stroke:var(--accent);stroke-width:1.5;vector-effect:non-scaling-stroke}
a{color:var(--accent)}
"""


def _page(title: str, body: str, refresh: bool = False) -> HTMLResponse:
    meta = '<meta http-equiv="refresh" content="60">' if refresh else ""
    return HTMLResponse(
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">{meta}'
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>"
        f'<nav><a href="/">Live</a><a href="/research">Research</a></nav>{body}</main></body></html>'
    )


def _rows(repo: Repo, stmt) -> list[dict]:
    with repo.engine.begin() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings()]


def state(repo: Repo) -> dict:
    now = _now()
    hb = repo.get_state("heartbeat")
    metrics = repo.get_state("metrics", {}) or {}
    eq = _rows(repo, select(m.equity).order_by(m.equity.c.ts.desc()).limit(24 * 90))[::-1]
    positions = repo.open_positions()
    signals = _rows(
        repo,
        select(m.signals)
        .where(m.signals.c.stage != "core")
        .order_by(m.signals.c.bar_time.desc())
        .limit(40),
    )
    core_rej = _rows(
        repo,
        select(m.signals)
        .where(m.signals.c.stage == "core")
        .order_by(m.signals.c.bar_time.desc())
        .limit(15),
    )
    closed = _rows(
        repo,
        select(m.positions)
        .where(m.positions.c.status == "CLOSED")
        .order_by(m.positions.c.closed_at.desc())
        .limit(30),
    )
    events = _rows(repo, select(m.events).order_by(m.events.c.ts.desc()).limit(25))
    last = eq[-1] if eq else {}
    return {
        "now": now,
        "heartbeat": hb,
        "heartbeat_age_s": (now - hb) / 1000 if hb else None,
        "healthy": bool(hb and now - hb <= HEARTBEAT_STALE_MS),
        "metrics": metrics,
        "halted": bool(repo.get_state("halted", False)),
        "stage": (repo.get_state("stage") or {}).get("name", metrics.get("stage")),
        "guard": repo.get_state("guard", {}),
        "equity": last.get("equity"),
        "peak": last.get("peak"),
        "drawdown": last.get("drawdown"),
        "realized": repo.realized_net(),
        "equity_curve": [(r["ts"], r["equity"]) for r in eq],
        "positions": positions,
        "signals": signals,
        "core_rejections": core_rej,
        "closed": closed,
        "events": events,
    }


def _dashboard(s: dict) -> str:
    mode = s["metrics"].get("mode", "?")
    hb = s["heartbeat_age_s"]
    health = (
        f'<span class="pill ok">heartbeat {hb:.0f}s ago</span>'
        if s["healthy"]
        else f'<span class="pill bad">heartbeat {"never" if hb is None else f"{hb / 60:.0f} min ago"}</span>'
    )
    halted = '<span class="pill bad">HALTED: manual review</span>' if s["halted"] else ""
    paused = s["guard"].get("paused_until", 0) or 0
    pause = (
        f'<span class="pill warn">paused until {_ts(paused)}</span>' if paused > s["now"] else ""
    )
    halve = (
        '<span class="pill warn">size halved</span>'
        if s["guard"].get("halved_dd") or (s["guard"].get("halve_trades_left") or 0)
        else ""
    )
    kpis = [
        ("Equity", _num(s["equity"])),
        ("Peak", _num(s["peak"])),
        ("Drawdown", _num(s["drawdown"], ".1%") if s["drawdown"] is not None else "–"),
        ("Realized PnL", _num(s["realized"], "+,.2f")),
        ("Open positions", str(len(s["positions"]))),
        ("Last hour", _ts(s["metrics"].get("last_hour"))),
        ("API weight/min", _num(s["metrics"].get("api_weight_used"), "d")),
    ]
    kpi_html = "".join(f'<div class="kpi"><span>{k}</span><b>{v}</b></div>' for k, v in kpis)
    for p in s["positions"]:
        p["_opened"] = _ts(p["opened_at"])
        risk = abs((p["entry_price"] or 0) - (p["stop_initial"] or 0))
        p["_stop_r"] = (
            _num(abs((p["stop"] or 0) - (p["entry_price"] or 0)) / risk, ".2f") if risk else "–"
        )
    for r in s["signals"] + s["core_rejections"]:
        r["_time"] = _ts(r["bar_time"])
        cls = "ok" if r["decision"] == "accepted" else "muted"
        r["_decision"] = f'<span class="{cls}">{html.escape(r["decision"] or "")}</span>'
        r["_why"] = html.escape(f"{r['stage']}: {r['reject_reason']}" if r["reject_reason"] else "")
    for c in s["closed"]:
        c["_closed"] = _ts(c["closed_at"])
        risk = (c["qty"] or 0) * abs((c["entry_price"] or 0) - (c["stop_initial"] or 0))
        c["_r"] = _num((c["pnl"] or 0) / risk, "+.2f") if risk else "–"
    for e in s["events"]:
        e["_time"] = _ts(e["ts"])
        e["_what"] = html.escape(str(e["payload"])[:160])
    return (
        f'<header><h1>zone-trend</h1><span class="pill">{html.escape(mode)}</span>'
        f'<span class="pill">stage {html.escape(str(s["stage"] or "–"))}</span>{health}{halted}'
        f'{pause}{halve}</header><div class="kpis">{kpi_html}</div>'
        f"<h2>Equity</h2>{_svg(s['equity_curve'])}"
        "<h2>Open positions</h2>"
        + _table(
            s["positions"],
            [
                ("symbol", "Symbol"),
                ("side", "Side"),
                ("_opened", "Opened"),
                ("entry_price", "Entry"),
                ("stop", "Stop"),
                ("_stop_r", "Stop dist (R)"),
                ("tp1", "TP1"),
                ("tp2", "TP2"),
                ("qty_open", "Qty open"),
                ("tp1_done", "TP1 hit"),
                ("bars_open", "Bars"),
            ],
            "flat",
        )
        + "<h2>Signals</h2>"
        + _table(
            s["signals"],
            [
                ("_time", "Bar (UTC)"),
                ("symbol", "Symbol"),
                ("side", "Side"),
                ("_decision", "Decision"),
                ("_why", "Reason"),
                ("entry_ref", "Entry"),
                ("stop", "Stop"),
                ("tp1", "TP1"),
                ("quality", "Q"),
            ],
        )
        + "<h2>Recent core rejections</h2>"
        + _table(
            s["core_rejections"],
            [("_time", "Bar (UTC)"), ("symbol", "Symbol"), ("side", "Side"), ("_why", "Reason")],
        )
        + "<h2>Closed trades</h2>"
        + _table(
            s["closed"],
            [
                ("_closed", "Closed (UTC)"),
                ("symbol", "Symbol"),
                ("side", "Side"),
                ("entry_price", "Entry"),
                ("pnl", "Net PnL"),
                ("_r", "R"),
                ("fees", "Fees"),
                ("funding", "Funding"),
                ("exit_reason", "Exit"),
            ],
        )
        + "<h2>Alerts and events</h2>"
        + _table(
            s["events"],
            [("_time", "Time (UTC)"), ("level", "Level"), ("kind", "Kind"), ("_what", "Detail")],
        )
    )


def _research(runs_dir: Path) -> str:
    parts = ["<header><h1>Research</h1></header>"]
    wfs = sorted(runs_dir.glob("*/acceptance.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for acc in wfs:
        rep = json.loads(acc.read_text())
        rows = []
        for c in rep["criteria"]:
            cls = {"pass": "ok", "fail": "bad"}.get(c["status"], "warn")
            rows.append(
                {
                    "_c": html.escape(c["criterion"]),
                    "_v": html.escape(str(c["value"])),
                    "_t": html.escape(c["threshold"]),
                    "_s": f'<span class="pill {cls}">{c["status"]}</span>',
                }
            )
        verdict = rep["verdict"]
        vcls = {"pass": "ok", "fail": "bad"}.get(verdict, "warn")
        parts.append(
            f'<h2>{html.escape(acc.parent.name)} <span class="pill {vcls}">{verdict}</span></h2>'
            + _table(
                rows, [("_c", "Criterion"), ("_v", "Value"), ("_t", "Threshold"), ("_s", "Status")]
            )
        )
    reports = sorted(runs_dir.glob("*/report.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    rows = []
    for r in reports[:100]:
        meta_path = r.parent / "meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        st = meta.get("summary", {})
        rows.append(
            {
                "_run": f'<a href="/runs/{r.parent.name}/report.html">{html.escape(r.parent.name)}</a>',
                "_kind": html.escape(str(meta.get("kind", ""))),
                "_range": html.escape(" → ".join(meta.get("range", []))),
                "_syms": str(len(meta.get("symbols", []))),
                "trades": st.get("trades"),
                "profit_factor": st.get("profit_factor"),
                "expectancy_r": st.get("expectancy_r"),
                "max_drawdown": st.get("max_drawdown"),
                "sharpe_daily": st.get("sharpe_daily"),
            }
        )
    parts.append("<h2>Backtests</h2>")
    parts.append(
        _table(
            rows,
            [
                ("_run", "Run"),
                ("_kind", "Kind"),
                ("_range", "Range"),
                ("_syms", "Symbols"),
                ("trades", "Trades"),
                ("profit_factor", "PF"),
                ("expectancy_r", "Exp. R"),
                ("max_drawdown", "Max DD"),
                ("sharpe_daily", "Sharpe"),
            ],
            "no runs yet",
        )
    )
    return "".join(parts)


def _metrics(s: dict) -> str:
    met = s["metrics"]
    lines = []

    def g(name: str, value: Any, help_: str) -> None:
        if value is None:
            return
        lines.extend(
            [f"# HELP zt_{name} {help_}", f"# TYPE zt_{name} gauge", f"zt_{name} {float(value)}"]
        )

    g("equity", s["equity"], "Equity in USDT (zone-trend positions only)")
    g("peak_equity", s["peak"], "Peak equity in USDT")
    g("drawdown", s["drawdown"], "Drawdown from peak, fraction")
    g("realized_pnl", s["realized"], "Realized net PnL in USDT")
    g("open_positions", len(s["positions"]), "Open positions")
    g("halted", int(s["halted"]), "1 when halted for manual review")
    g("heartbeat_age_seconds", s["heartbeat_age_s"], "Seconds since the last heartbeat")
    g("last_processed_hour_ms", met.get("last_hour"), "Close time of the last processed hour")
    g("api_weight_used", met.get("api_weight_used"), "Binance request weight used this minute")
    return "\n".join(lines) + "\n"


def create_app(db_url: str, runs_dir: Path) -> FastAPI:
    repo = Repo(db_url)
    app = FastAPI(title="zone-trend status", docs_url=None, redoc_url=None)
    runs_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/runs", StaticFiles(directory=runs_dir), name="runs")

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        return _page("zone-trend", _dashboard(state(repo)), refresh=True)

    @app.get("/research", response_class=HTMLResponse)
    def research() -> HTMLResponse:
        return _page("zone-trend research", _research(runs_dir))

    @app.get("/api/state")
    def api_state() -> JSONResponse:
        return JSONResponse(json.loads(json.dumps(state(repo), default=str)))

    @app.get("/health")
    def health() -> JSONResponse:
        s = state(repo)
        body = {
            "healthy": s["healthy"],
            "heartbeat_age_s": s["heartbeat_age_s"],
            "halted": s["halted"],
        }
        return JSONResponse(body, status_code=200 if s["healthy"] else 503)

    @app.get("/health/live")
    def live() -> dict:
        return {"ok": True}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        return _metrics(state(repo))

    return app
