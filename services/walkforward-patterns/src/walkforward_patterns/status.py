"""Read-only status page (127.0.0.1 only; reach it with an ssh tunnel). SPEC §9 per-strategy dashboard."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config
from .journal import Journal


def snapshot(j: Journal, cfg: Config) -> dict:
    started = j.get("started_at") or datetime.now(UTC).isoformat()
    months = max((datetime.now(UTC) - datetime.fromisoformat(started)).days / 30.44, 1e-9)
    disabled = j.disabled()
    open_by = {r["strategy"]: dict(r) for r in j.open_trades()}
    rows = []
    for st in cfg.strategies:
        tr = j.closed_trades(st.id)
        rs = [float(t["r_net"] or 0) for t in tr]
        cum = peak = dd = 0.0
        for r in rs:
            cum += r
            peak = max(peak, cum)
            dd = min(dd, cum - peak)
        rate = len(rs) / months
        bt_rate = float(st.backtest.get("per_month") or 0)
        rows.append({
            "id": st.id, "name": st.name, "symbol": st.symbol, "lots": st.lots, "stop_pips": st.stop_pips,
            "trades": len(rs), "win": (sum(r > 0 for r in rs) / len(rs)) if rs else None, "cum_R": cum,
            "mean_R": cum / len(rs) if rs else None, "dd_R": dd,
            "pips": sum(float(t["pips"] or 0) for t in tr), "net_usd": sum(float(t["net_usd"] or 0) for t in tr),
            "rate_per_month": rate, "backtest_per_month": bt_rate, "backtest_mean_R": st.backtest.get("mean_R"),
            "rate_flag": months >= 3 and bt_rate > 0 and not (0.5 * bt_rate <= rate <= 2 * bt_rate),
            "status": "killed" if st.id in disabled else ("enabled" if st.enabled else "disabled"),
            "kill_reason": disabled.get(st.id), "open": open_by.get(st.id),
        })
    eq = j.get("equity_est")
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "started_at": started,
        "heartbeat": j.get("heartbeat"), "equity": json.loads(eq) if eq else None, "strategies": rows,
        "recent_trades": [dict(r) for r in j.trades(50)], "events": [dict(r) for r in j.events(50)],
    }


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Walk-forward top 20</title><style>
:root{--bg:#fff;--fg:#1d1d1f;--mut:#6e6e73;--line:#e5e5ea;--pos:#0a7d32;--neg:#c0262d;--warn:#a15c00}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececf0;--mut:#9a9aa2;--line:#2c2c31;--pos:#4cc06f;--neg:#f0626a;--warn:#e0a040}}
body{background:var(--bg);color:var(--fg);font:13px/1.4 -apple-system,system-ui,sans-serif;margin:16px}
h1{font-size:18px;margin:0 0 4px}.mut{color:var(--mut)}table{border-collapse:collapse;width:100%;margin:12px 0}
th,td{padding:4px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child,td.l{text-align:left}.pos{color:var(--pos)}.neg{color:var(--neg)}.warn{color:var(--warn)}
.wrap{overflow-x:auto}</style></head><body><h1>Walk-forward top 20 · FTMO demo</h1><div id="hdr" class="mut"></div>
<div class="wrap"><table id="s"></table></div><h2 style="font-size:15px">Recent trades</h2><div class="wrap"><table id="t"></table></div>
<h2 style="font-size:15px">Events</h2><div class="wrap"><table id="e"></table></div>
<script>
const f=(x,d=2)=>x==null?'':(+x).toFixed(d), c=x=>x==null?'':(x>0?'pos':x<0?'neg':'');
async function load(){const s=await (await fetch('api/state')).json();
const q=s.equity||{};document.getElementById('hdr').textContent=`heartbeat ${s.heartbeat||'-'} · since ${s.started_at.slice(0,10)} · equity est $${f(q.equity,0)} (realised ${f(q.realised,0)}, floating ${f(q.floating,0)}) · FTMO limits record-only`;
let h='<tr><th>id</th><th>strategy</th><th>sym</th><th>lots</th><th>trades</th><th>win</th><th>cum R</th><th>mean R</th><th>bt mean R</th><th>DD R</th><th>pips</th><th>net $</th><th>/month</th><th>bt /month</th><th>status</th><th>open</th></tr>';
for(const r of s.strategies){h+=`<tr><td>${r.id}</td><td class="l">${r.name}</td><td>${r.symbol}</td><td>${r.lots??''}</td><td>${r.trades}</td><td>${r.win==null?'':f(100*r.win,0)+'%'}</td><td class="${c(r.cum_R)}">${f(r.cum_R)}</td><td class="${c(r.mean_R)}">${f(r.mean_R,3)}</td><td>${f(r.backtest_mean_R,3)}</td><td>${f(r.dd_R)}</td><td class="${c(r.pips)}">${f(r.pips,1)}</td><td class="${c(r.net_usd)}">${f(r.net_usd)}</td><td class="${r.rate_flag?'warn':''}">${f(r.rate_per_month,1)}</td><td>${f(r.backtest_per_month,1)}</td><td class="${r.status=='killed'?'neg':''}">${r.status}</td><td>${r.open?(r.open.side>0?'BUY ':'SELL ')+(r.open.fill_price??r.open.status):''}</td></tr>`}
document.getElementById('s').innerHTML=h;
h='<tr><th>id</th><th>strategy</th><th>side</th><th>status</th><th>entry (UTC)</th><th>fill</th><th>exit (UTC)</th><th>exit</th><th>reason</th><th>R</th><th>net $</th></tr>';
for(const t of s.recent_trades){h+=`<tr><td>${t.id}</td><td>${t.strategy}</td><td>${t.side>0?'BUY':'SELL'}</td><td>${t.status}</td><td>${(t.entry_utc||'').slice(0,16)}</td><td>${t.fill_price??''}</td><td>${(t.exit_utc||'').slice(0,16)}</td><td>${t.exit_price??''}</td><td>${t.exit_reason??''}</td><td class="${c(t.r_net)}">${f(t.r_net)}</td><td class="${c(t.net_usd)}">${f(t.net_usd)}</td></tr>`}
document.getElementById('t').innerHTML=h;
h='<tr><th>at</th><th>level</th><th>kind</th><th>message</th></tr>';
for(const e of s.events){h+=`<tr><td>${e.at}</td><td class="${e.level=='error'?'neg':e.level=='warn'?'warn':''}">${e.level}</td><td>${e.kind}</td><td class="l">${e.message}</td></tr>`}
document.getElementById('e').innerHTML=h}
load();setInterval(load,30000);
</script></body></html>"""


def serve(j: Journal, cfg: Config, host: str, port: int) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.split("?")[0] in ("/api/state", "/api/state/"):
                body = json.dumps(snapshot(j, cfg), default=str).encode()
                ctype = "application/json"
            elif self.path.split("?")[0] in ("/", "/index.html"):
                body = PAGE.encode()
                ctype = "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer((host, port), H)
    threading.Thread(target=srv.serve_forever, daemon=True, name="status").start()
    return srv
