"""The status page: one static document; it polls ``/api/summary`` every 10 s."""

STATUS_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>poly-kalshi status</title>
<style>
:root { --bg:#f7f7f5; --panel:#ffffff; --ink:#1d1d1b; --muted:#6b6b66; --line:#e3e3de; --good:#1f7a4d; --bad:#b3261e; --warn:#9a6700; }
@media (prefers-color-scheme: dark) { :root { --bg:#141413; --panel:#1d1d1b; --ink:#ededea; --muted:#a3a39c; --line:#33332f; --good:#5cc58f; --bad:#f2847b; --warn:#e0b04a; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 ui-sans-serif, system-ui, -apple-system, sans-serif; }
main { max-width:1100px; margin:0 auto; padding:16px; }
h1 { font-size:18px; margin:0 0 4px; } h2 { font-size:14px; margin:0 0 8px; color:var(--muted); font-weight:600; text-transform:uppercase; letter-spacing:.04em; }
.sub { color:var(--muted); margin-bottom:16px; }
.grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit, minmax(240px, 1fr)); margin-bottom:12px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; overflow-x:auto; }
.kv { display:flex; justify-content:space-between; gap:8px; } .kv span:first-child { color:var(--muted); }
table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
th, td { text-align:right; padding:4px 6px; border-bottom:1px solid var(--line); white-space:nowrap; }
th:first-child, td:first-child { text-align:left; } th { color:var(--muted); font-weight:500; }
.good { color:var(--good); } .bad { color:var(--bad); } .warn { color:var(--warn); } .muted { color:var(--muted); }
</style>
</head>
<body>
<main>
<h1>poly-kalshi</h1>
<div class="sub" id="sub">loading…</div>
<div class="grid">
  <div class="card"><h2>Ledger</h2><div id="ledger"></div></div>
  <div class="card"><h2>Feeds</h2><div id="feeds"></div></div>
  <div class="card"><h2>Gates</h2><div id="gates"></div></div>
</div>
<div class="card" style="margin-bottom:12px"><h2>Windows</h2><table id="assets"></table></div>
<div class="card" style="margin-bottom:12px"><h2>Fill price and net ¢/share by band</h2><table id="bands"></table></div>
<div class="card"><h2>Recent</h2><table id="recent"></table></div>
</main>
<script>
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "–").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const f = (v, d = 2) => (v === null || v === undefined) ? "–" : Number(v).toFixed(d);
const kv = (rows) => rows.map(([k, v, cls]) => `<div class="kv"><span>${esc(k)}</span><span class="${cls || ""}">${esc(v)}</span></div>`).join("");
const verdict = (v) => v === null || v === undefined ? ["pending", "muted"] : v ? ["pass", "good"] : ["FAIL", "bad"];
const mid = (q) => q && q.valid && q.bid !== null && q.ask !== null ? (q.bid + q.ask) / 2 : null;

async function tick() {
  let body;
  try { body = await (await fetch("api/summary")).json(); } catch (e) { $("sub").textContent = "status page cannot reach its API"; return; }
  const live = body.live, g = body.gates || {};
  $("sub").innerHTML = live
    ? `mode <b>${esc(live.mode)}</b> · host ${esc(live.host)} · gap ≥ ${f(live.params.gap_threshold)} · cap ${f(live.params.max_entry_px)} · $${f(live.params.notional_usd, 0)}/entry · ${live.risk && live.risk.halted ? '<span class="bad">HALTED</span>' : '<span class="good">running</span>'}`
    : '<span class="bad">service unreachable</span>';
  const L = (live && live.ledger) || {};
  $("ledger").innerHTML = kv([["cash", "$" + f(L.cash)], ["open positions", L.open_positions], ["open notional", "$" + f(L.open_notional)],
    ["realised today", "$" + f(L.realised_today), (L.realised_today || 0) < 0 ? "bad" : "good"], ["realised total", "$" + f(L.realised_total), (L.realised_total || 0) < 0 ? "bad" : "good"],
    ["balance rejects", L.rejects, L.rejects ? "bad" : ""]]);
  const feeds = (live && live.feeds) || {}, age = (live && live.feed_age_s) || {};
  $("feeds").innerHTML = kv([["Kalshi", (feeds.kalshi && feeds.kalshi.connected ? "up" : "DOWN") + ` · ${f(age.k, 1)}s`, feeds.kalshi && feeds.kalshi.connected ? "good" : "bad"],
    ["Polymarket", (feeds.poly && feeds.poly.connected ? "up" : "DOWN") + ` · ${f(age.p, 1)}s`, feeds.poly && feeds.poly.connected ? "good" : "bad"],
    ["Kalshi seq gaps", feeds.kalshi ? feeds.kalshi.seq_gaps : "–"], ["recorder lines", live && live.recorder ? live.recorder.lines_written : "–"],
    ["disk free", live && live.recorder ? f(live.recorder.disk_free_gb, 1) + " GB" : "–"]]);
  const gg = g.gates || {}, v = g.vwap || {};
  $("gates").innerHTML = kv([["entries (≤ cap)", `${g.in_band_n ?? 0} / ${g.gate_min_n ?? 300}`], ["avg / p90 fill", `${f(v.avg, 3)} / ${f(v.p90, 3)}`, verdict(gg.fill_price)[1]],
    ["fill rate", f((g.fill_rate ?? 0) * 100, 1) + "%"], ["net ¢/share", f(g.net_cents_per_share)], ["$ / entry", f(g.dollars_per_entry)],
    ["day-t (" + (g.days ?? 0) + "d, " + (g.days_positive ?? 0) + " +)", f(g.day_t), verdict(gg.day_t)[1]], ["venue disagreement", g.venue_disagreement == null ? "–" : f(g.venue_disagreement * 100, 1) + "%"]]);
  const A = (live && live.assets) || {};
  $("assets").innerHTML = "<tr><th>asset</th><th>sec</th><th>Kalshi mid</th><th>Poly YES</th><th>Poly NO</th><th>gap</th><th>max |gap|</th><th>state</th></tr>" +
    Object.entries(A).map(([n, a]) => `<tr><td>${esc(n)}</td><td>${f(a.sec_into, 0)}</td><td>${f(mid(a.kalshi), 3)}</td>
      <td>${a.poly_yes && a.poly_yes.valid ? f(a.poly_yes.bid) + " / " + f(a.poly_yes.ask) : "–"}</td><td>${a.poly_no && a.poly_no.valid ? f(a.poly_no.bid) + " / " + f(a.poly_no.ask) : "–"}</td>
      <td>${f(a.last_gap, 3)}</td><td>${f(a.max_abs_gap, 3)}</td><td class="${a.identified ? "" : "bad"}">${esc(a.identified ? (a.action || (a.traded ? "traded" : "watching")) : "unidentified")}</td></tr>`).join("");
  const B = g.bands || {};
  $("bands").innerHTML = "<tr><th>entry band</th><th>n</th><th>net ¢/share</th><th>manual excess ¢</th></tr>" +
    Object.entries(B).map(([b, x], i) => `<tr><td>${esc(b)}</td><td>${x.n}</td><td class="${x.net_cents == null ? "" : x.net_cents < 0 ? "bad" : "good"}">${f(x.net_cents)}</td><td class="muted">${[16.2, 14.9, 12.6, 8.3, 1.0, 1.6, -0.4, -0.6][i]}</td></tr>`).join("");
  const R = (live && live.recent) || [];
  $("recent").innerHTML = "<tr><th>type</th><th>asset</th><th>window</th><th>action / side</th><th>px</th><th>shares</th><th>gap</th><th>P&amp;L</th></tr>" +
    R.map((r) => `<tr><td>${esc(r.type)}</td><td>${esc(r.asset)}</td><td>${r.open_unix ? new Date(r.open_unix * 1000).toISOString().slice(5, 16).replace("T", " ") : "–"}</td>
      <td>${esc(r.type === "decision" ? r.action + " " + r.side + (r.blocked ? " (" + r.blocked + ")" : "") : r.side)}</td><td>${f(r.vwap ?? r.entry_px, 3)}</td>
      <td>${f(r.shares ?? r.contracts, 0)}</td><td>${f(r.gap, 3)}</td><td class="${r.pnl == null ? "" : r.pnl < 0 ? "bad" : "good"}">${f(r.pnl)}</td></tr>`).join("");
}
tick(); setInterval(tick, 10000);
</script>
</body>
</html>
"""
