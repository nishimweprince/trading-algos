"""The status page: one static document (no libraries); it polls ``/api/summary`` every 10 s."""

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
svg.curve { width:100%; height:140px; display:block; }
svg.curve .zero { stroke:var(--line); stroke-dasharray:3 3; } svg.curve .line { fill:none; stroke-width:1.5; }
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
<div class="card" style="margin-bottom:12px"><h2>Paper equity (cumulative P&amp;L, settled trades)</h2><div id="curve" class="muted">no settled trades yet</div></div>
<div class="card" style="margin-bottom:12px"><h2>Daily paper results (UTC)</h2><table id="daily"></table></div>
<div class="card" style="margin-bottom:12px"><h2>Paper trades (last 50)</h2><table id="trades"></table></div>
<div class="card" style="margin-bottom:12px"><h2>Windows</h2><table id="assets"></table></div>
<div class="card" style="margin-bottom:12px"><h2>Fill price and net ¢/share by band</h2><table id="bands"></table></div>
<div class="card"><h2>Recent engine events</h2><table id="recent"></table></div>
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
  const live = body.live, g = body.gates || {}, res = body.results || {};
  const dataAge = body.fetched_at ? Math.round(Date.now() / 1000 - body.fetched_at) : null;
  $("sub").innerHTML = live
    ? `mode <b>${esc(live.mode)}</b> · host ${esc(live.host)} · gap ≥ ${f(live.params.gap_threshold)} · cap ${f(live.params.max_entry_px)} · $${f(live.params.notional_usd, 0)}/entry · ${live.risk && live.risk.halted ? '<span class="bad">HALTED</span>' : '<span class="good">running</span>'} · <span class="${dataAge > 30 ? "bad" : "muted"}">data ${dataAge}s old</span>`
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
  const C = res.curve || [];
  if (C.length > 1) {
    const W = 1000, H = 140, P = 6, vals = C.map((c) => c[1]), lo = Math.min(0, ...vals), hi = Math.max(0, ...vals), span = hi - lo || 1;
    const x = (i) => P + (i / (C.length - 1)) * (W - 2 * P), y = (v) => H - P - ((v - lo) / span) * (H - 2 * P);
    const pts = C.map((c, i) => `${x(i).toFixed(1)},${y(c[1]).toFixed(1)}`).join(" "), last = vals[vals.length - 1];
    $("curve").innerHTML = `<svg class="curve" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="cumulative paper P&L">
      <line class="zero" x1="0" x2="${W}" y1="${y(0)}" y2="${y(0)}"/><polyline class="line" points="${pts}" stroke="${last < 0 ? "var(--bad)" : "var(--good)"}"/></svg>
      <div class="kv"><span>${C.length} settled · since ${new Date(C[0][0] * 1000).toISOString().slice(0, 16).replace("T", " ")}</span><span class="${last < 0 ? "bad" : "good"}">$${f(last)}</span></div>`;
  }
  const D = res.daily || [];
  $("daily").innerHTML = "<tr><th>day</th><th>entries</th><th>skips</th><th>blocked</th><th>filled</th><th>no fill</th><th>avg fill</th><th>win %</th><th>net ¢/sh</th><th>fees</th><th>P&amp;L</th><th>cum.</th></tr>" +
    (D.length ? D.map((d) => `<tr><td>${esc(d.day)}</td><td>${d.enter}</td><td>${d.skip}</td><td class="${d.blocked ? "warn" : ""}">${d.blocked}</td><td>${d.filled}</td><td>${d.no_fill}</td>
      <td class="${d.avg_vwap > 0.6 ? "bad" : ""}">${f(d.avg_vwap, 3)}</td><td>${d.win_rate == null ? "–" : f(d.win_rate * 100, 1)}</td><td>${f(d.net_cents)}</td><td>${f(d.fees)}</td>
      <td class="${d.pnl < 0 ? "bad" : "good"}">${f(d.pnl)}</td><td class="${d.cumulative < 0 ? "bad" : "good"}">${f(d.cumulative)}</td></tr>`).join("") : '<tr><td class="muted">no decisions yet</td></tr>');
  const T = res.trades || [];
  $("trades").innerHTML = "<tr><th>time (UTC)</th><th>asset</th><th>side</th><th>gap</th><th>sec</th><th>touch</th><th>fill</th><th>shares</th><th>fee</th><th>outcome</th><th>P&amp;L</th></tr>" +
    (T.length ? T.map((t) => `<tr><td>${new Date(t.t_ns / 1e6).toISOString().slice(5, 19).replace("T", " ")}</td><td>${esc(t.asset)}</td><td>${esc(t.side)}</td><td>${f(t.gap, 3)}</td><td>${f(t.sec_into, 0)}</td>
      <td>${f(t.touch_px, 2)}</td><td>${f(t.vwap, 3)}</td><td>${f(t.shares, 0)}/${f(t.ordered, 0)}</td><td>${f(t.fee)}</td><td>${esc(t.outcome ?? "open")}</td>
      <td class="${t.pnl == null ? "muted" : t.pnl < 0 ? "bad" : "good"}">${t.pnl == null ? "–" : f(t.pnl)}</td></tr>`).join("") : '<tr><td class="muted">no fills yet</td></tr>');
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
