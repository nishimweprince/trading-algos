"""``GET /dashboard``: one self-contained page over the authenticated JSON routes.

Every signal with its probability and the action taken, every cycle with its
result, the policy state per symbol, and the day's summary, refreshed every
two seconds. Reach it through the SSH tunnel like the API; the page asks for
the API key once and keeps it in this tab only (sessionStorage).
"""

from __future__ import annotations

__all__ = ["DASHBOARD_HTML"]

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OFI Scalper</title>
<style>
:root { --bg:#f7f7f5; --fg:#1d1d1b; --muted:#6b6b66; --card:#fff;
        --line:#e4e4df;
        --good:#1b7a3d; --bad:#b3261e; --warn:#8a5a00; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#151514; --fg:#ececea; --muted:#9a9a94; --card:#1e1e1c;
          --line:#2e2e2b;
          --good:#5cc27f; --bad:#ff8a80; --warn:#e0b050; } }
* { box-sizing: border-box; }
body { margin:0; padding:16px; background:var(--bg); color:var(--fg);
       font:14px/1.45 ui-sans-serif, system-ui, sans-serif; }
h1 { font-size:18px; margin:0 0 12px; }
h2 { font-size:14px; margin:0 0 8px; color:var(--muted); font-weight:600; }
.grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px;
        overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
th, td { text-align:left; padding:4px 6px; border-bottom:1px solid var(--line);
         white-space:nowrap; }
th { color:var(--muted); font-weight:500; }
.good { color:var(--good); } .bad { color:var(--bad); } .warn { color:var(--warn); }
.kv { display:grid; grid-template-columns:auto 1fr; gap:2px 12px; }
.kv div:nth-child(odd) { color:var(--muted); }
#err { color:var(--bad); min-height:1.4em; }
</style>
</head>
<body>
<h1>OFI scalper <span id="mode" class="warn"></span></h1>
<div id="err"></div>
<div class="grid">
  <div class="card"><h2>State</h2><div id="state" class="kv"></div></div>
  <div class="card"><h2>Today</h2><div id="summary" class="kv"></div></div>
</div>
<div class="card" style="margin-top:12px"><h2>Policy</h2><div id="policy"></div></div>
<div class="card" style="margin-top:12px"><h2>Signals</h2><div id="signals"></div></div>
<div class="card" style="margin-top:12px"><h2>Cycles</h2><div id="trades"></div></div>
<script>
const KEY = "ofi-api-key";
function apiKey() {
  let key = null;
  try { key = sessionStorage.getItem(KEY); } catch (e) {}
  if (!key) {
    key = prompt("API key for this scalper") || "";
    try { sessionStorage.setItem(KEY, key); } catch (e) {}
  }
  return key;
}
async function get(path) {
  const r = await fetch(path, { headers: { "X-API-Key": apiKey() } });
  if (r.status === 401) { try { sessionStorage.removeItem(KEY); } catch (e) {} }
  if (!r.ok) throw new Error(path + ": HTTP " + r.status);
  return r.json();
}
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" };
const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ESC[c]);
const num = (v, d = 2) => (v === null || v === undefined) ? "" : Number(v).toFixed(d);
const cls = (v) => v > 0 ? "good" : v < 0 ? "bad" : "";
function kv(el, pairs) {
  el.innerHTML = pairs.map(([k, v]) => `<div>${esc(k)}</div><div>${v}</div>`).join("");
}
function table(rows, cols) {
  if (!rows.length) return "<div>none yet</div>";
  return "<table><tr>" + cols.map((c) => `<th>${esc(c[0])}</th>`).join("") + "</tr>" +
    rows.map((r) => "<tr>" + cols.map((c) => `<td>${c[1](r)}</td>`).join("") + "</tr>").join("") +
    "</table>";
}
async function refresh() {
  try {
    const today = new Date().toISOString().slice(0, 10);
    const [status, signals, trades] = await Promise.all([
      get("/v1/status"),
      get("/v1/signals?limit=50"),
      get("/v1/trades?date=" + today + "&limit=50"),
    ]);
    document.getElementById("err").textContent = "";
    document.getElementById("mode").textContent = status.execution_mode;
    const risk = status.risk, bridge = status.bridge || {}, model = status.model || {};
    kv(document.getElementById("state"), [
      ["ready", status.ready ? "yes" : '<span class="bad">no</span>'],
      ["halted", risk.halted ? `<span class="bad">${esc(risk.halt.reason)}</span>` : "no"],
      ["pauses", esc(JSON.stringify(risk.pauses))],
      ["model", esc(model.version || "none")],
      ["scoring p50/p99 µs", model.scoring_latency
        ? `${model.scoring_latency.p50_us} / ${model.scoring_latency.p99_us}` : ""],
      ["venue ready", bridge.venue_ready ? "yes" : "no"],
      ["unknown outcomes", esc(bridge.consecutive_unknown ?? "")],
      ["equity", num(risk.equity)],
    ]);
    const s = trades.summary;
    kv(document.getElementById("summary"), [
      ["signals / entries / trades", `${s.signals} / ${s.entries} / ${s.trades}`],
      ["net P&L", `<span class="${cls(s.net_usd)}">${num(s.net_usd, 4)}</span>`],
      ["fees", num(s.fees_usd, 4)],
      ["win rate", s.win_rate === null ? "" : num(s.win_rate * 100, 1) + "%"],
      ["maker fill rate", s.maker_fill_rate === null ? "" : num(s.maker_fill_rate * 100, 1) + "%"],
      ["largest loss", num(s.largest_loss_usd, 4)],
      ["adverse bp 1/5/30s", ["1s", "5s", "30s"].map((k) => num(s.adverse_bp_mean[k])).join(" / ")],
      ["Brier (own fills)", num(s.brier, 4)],
    ]);
    const policy = Object.entries(bridge.policy || {}).map(([sym, p]) => ({ sym, ...p }));
    document.getElementById("policy").innerHTML = table(policy, [
      ["symbol", (r) => esc(r.sym)], ["phase", (r) => esc(r.phase)],
      ["cycle", (r) => esc(r.cycle ? r.cycle.cycle_id : "")],
      ["side", (r) => esc(r.cycle ? r.cycle.side : "")],
      ["p", (r) => r.cycle ? num(r.cycle.p, 3) : ""],
    ]);
    document.getElementById("signals").innerHTML = table(signals.signals, [
      ["time", (r) => esc((r.at || "").slice(11, 23))], ["symbol", (r) => esc(r.symbol)],
      ["side", (r) => esc(r.side)], ["p", (r) => num(r.p, 3)],
      ["threshold", (r) => num(r.threshold, 3)],
      ["edge bp", (r) => num(r.edge_bp)], ["cost bp", (r) => num(r.cost_bp)],
      ["action", (r) =>
        `<span class="${r.action === "enter" ? "good" : ""}">${esc(r.action)}</span>`],
    ]);
    document.getElementById("trades").innerHTML = table(trades.trades, [
      ["signal", (r) => esc((r.signal_at || "").slice(11, 23))], ["symbol", (r) => esc(r.symbol)],
      ["side", (r) => esc(r.side)], ["p", (r) => num(r.p, 3)],
      ["filled", (r) => r.filled ? "yes" : "no"],
      ["entry", (r) => num(r.entry_price, 2)], ["exit", (r) => num(r.exit_price, 2)],
      ["reason", (r) => esc(r.exit_reason)],
      ["net bp", (r) => `<span class="${cls(r.net_bp)}">${num(r.net_bp)}</span>`],
      ["net $", (r) => `<span class="${cls(r.net_usd)}">${num(r.net_usd, 4)}</span>`],
    ]);
  } catch (e) {
    document.getElementById("err").textContent = e.message;
  }
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""
