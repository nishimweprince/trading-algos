"""The status page: one static document (no libraries).

It polls ``/api/summary`` every 10 s and ``/api/series?since=`` every 5 s,
appending only new per-second price points to its own copy for the charts.
"""

STATUS_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>poly-kalshi status</title>
<style>
:root { --bg:#f7f7f5; --panel:#ffffff; --ink:#1d1d1b; --muted:#6b6b66; --line:#e3e3de; --good:#1f7a4d; --bad:#b3261e; --warn:#9a6700; --kalshi:#2563c9; --poly:#c2560c; --shade:rgba(120,120,110,.10); }
@media (prefers-color-scheme: dark) { :root { --bg:#141413; --panel:#1d1d1b; --ink:#ededea; --muted:#a3a39c; --line:#33332f; --good:#5cc58f; --bad:#f2847b; --warn:#e0b04a; --kalshi:#7aa7ff; --poly:#f39a52; --shade:rgba(200,200,190,.08); } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 ui-sans-serif, system-ui, -apple-system, sans-serif; }
main { max-width:1320px; margin:0 auto; padding:16px; }
h1 { font-size:18px; margin:0 0 4px; } h2 { font-size:14px; margin:0 0 8px; color:var(--muted); font-weight:600; text-transform:uppercase; letter-spacing:.04em; }
.sub { color:var(--muted); margin-bottom:16px; }
.grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit, minmax(240px, 1fr)); margin-bottom:12px; }
.grid.wide { grid-template-columns:repeat(auto-fit, minmax(min(100%, 470px), 1fr)); }
.card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; overflow-x:auto; }
.kv { display:flex; justify-content:space-between; gap:8px; } .kv span:first-child { color:var(--muted); }
table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
th, td { text-align:right; padding:4px 6px; border-bottom:1px solid var(--line); white-space:nowrap; }
th:first-child, td:first-child { text-align:left; } th { color:var(--muted); font-weight:500; }
svg.curve { width:100%; height:140px; display:block; }
svg.curve .zero { stroke:var(--line); stroke-dasharray:3 3; } svg.curve .line { fill:none; stroke-width:1.5; }
.head { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; flex-wrap:wrap; }
.btn { display:inline-block; padding:6px 12px; border:1px solid var(--line); border-radius:6px; background:var(--panel); color:var(--ink); text-decoration:none; font:inherit; cursor:pointer; }
.btn:hover { border-color:var(--muted); } .btn[aria-pressed="true"] { border-color:var(--ink); font-weight:600; }
.charts { display:grid; gap:16px; grid-template-columns:repeat(auto-fit, minmax(min(100%, 430px), 1fr)); }
.chart h3 { font-size:14px; margin:0 0 4px; display:flex; justify-content:space-between; align-items:baseline; gap:8px; font-weight:600; }
.chart h3 span { font-size:12px; }
.chart h3 span { font-weight:400; color:var(--muted); font-variant-numeric:tabular-nums; }
svg.mkt { width:100%; height:auto; display:block; } svg.mkt text { font-size:12px; fill:var(--muted); }
svg.mkt .grid { stroke:var(--line); } svg.mkt .k { stroke:var(--kalshi); } svg.mkt .p { stroke:var(--poly); } svg.mkt .g { stroke:var(--ink); }
svg.mkt .thr { stroke:var(--warn); stroke-dasharray:4 3; } svg.mkt polyline { fill:none; stroke-width:2; stroke-linejoin:round; } svg.mkt .g { stroke-width:1.5; } svg.mkt .shade { fill:var(--shade); }
.legend { display:flex; gap:14px; color:var(--muted); font-size:12px; margin:4px 0 10px; flex-wrap:wrap; }
.sw { display:inline-block; width:14px; height:3px; vertical-align:middle; margin-right:5px; border-radius:2px; }
.mkstate { table-layout:fixed; min-width:1180px; }
.mkstate td, .mkstate th { vertical-align:middle; padding:4px 5px; overflow:hidden; text-overflow:ellipsis; }
.badge { display:inline-block; padding:0 7px; border-radius:10px; font-size:12px; border:1px solid var(--warn); color:var(--warn); }
.card { min-height:48px; } .mkstate .num { font-variant-numeric:tabular-nums; }
.dbar { position:relative; width:72px; height:10px; background:var(--shade); border-radius:3px; display:inline-block; vertical-align:middle; }
.dbar i { position:absolute; top:0; bottom:0; border-radius:3px; }
.dbar b { position:absolute; top:-2px; bottom:-2px; width:1px; background:var(--warn); }
.dbar .mid { background:var(--muted); width:1px; top:0; bottom:0; }
.pill { display:inline-block; padding:1px 7px; border-radius:10px; font-size:12px; border:1px solid var(--line); }
.pill.enter { border-color:var(--good); color:var(--good); } .pill.skip { border-color:var(--warn); color:var(--warn); }
h3.section { font-size:12px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin:22px 0 8px; font-weight:600; border-bottom:1px solid var(--line); padding-bottom:4px; }
.alerts { border:1px solid var(--line); border-radius:8px; padding:8px 12px; margin:4px 0 4px; background:var(--panel); }
.alerts.ok { color:var(--good); } .alerts.bad { border-color:var(--bad); }
.alerts ul { margin:0; padding-left:18px; } .alerts li { color:var(--bad); }
.good { color:var(--good); } .bad { color:var(--bad); } .warn { color:var(--warn); } .muted { color:var(--muted); }
</style>
</head>
<body>
<main>
<div class="head"><div><h1>poly-kalshi</h1><div class="sub"><span id="sub">connecting…</span> · <span id="age" class="muted"></span> <span id="conn"></span></div></div>
  <a class="btn" href="api/export" download title="journal, events log, ledger, metrics, recording manifests and chart data in one zip">Export all data (.zip)</a></div>
<div class="alerts" id="alerts" role="status">checking…</div>

<h3 class="section">Performance</h3>
<div class="grid">
  <div class="card"><h2>Capital &amp; risk</h2><div id="capital"></div></div>
  <div class="card"><h2>Trade quality (settled)</h2><div id="quality"></div></div>
  <div class="card"><h2>Gates</h2><div id="gates"></div></div>
</div>

<h3 class="section">Live market</h3>
<div class="card" style="margin-bottom:12px">
  <div class="head"><h2>Market state · Δ = Kalshi YES mid − Polymarket YES mid</h2><span class="muted" id="state-age"></span></div>
  <table class="mkstate" id="mkstate"><tr><td class="muted">loading…</td></tr></table>
  <div class="muted" style="font-size:12px;margin-top:6px">Prices are bid–ask with the mid in brackets. "Rule now" applies the entry rule to the books as they stand, whether or not this window is already used; "skip" means the cheap leg is above the price cap. Δ bar: centre is 0, amber ticks are ± threshold, full width is ± 0.5. Ages are ms since that book last changed. A one-sided book ("no ask") is normal near expiry, when the outcome is all but decided; no entries are made in the last 60 s anyway.</div>
</div>
<div class="card" style="margin-bottom:12px">
  <div class="head"><h2>Market data · per second</h2><div><button class="btn" id="v-cur" aria-pressed="true">Current window</button> <button class="btn" id="v-prev" aria-pressed="false">Previous window</button></div></div>
  <div class="legend"><span><i class="sw" style="background:var(--kalshi)"></i>Kalshi YES mid (signal)</span><span><i class="sw" style="background:var(--poly)"></i>Polymarket YES mid</span>
    <span><i class="sw" style="background:var(--ink)"></i>gap = Kalshi − Polymarket</span><span><i class="sw" style="background:var(--warn)"></i>± threshold</span><span><i class="sw" style="background:var(--shade);height:10px"></i>no-entry seconds</span></div>
  <div class="charts" id="charts"><div class="muted">waiting for market data…</div></div>
</div>

<h3 class="section">Edge</h3>
<div class="grid">
  <div class="card"><h2>Signal check (settled)</h2><div id="signal"></div></div>
  <div class="card"><h2>Opportunity funnel · total (today)</h2><div id="funnel"></div></div>
</div>
<div class="grid wide">
  <div class="card"><h2>By asset</h2><table id="by-asset"></table></div>
  <div class="card"><h2>By seconds into window</h2><table id="by-sec"></table></div>
  <div class="card"><h2>By entry price band</h2><table id="bands"></table></div>
</div>

<h3 class="section">History</h3>
<div class="card" style="margin-bottom:12px"><h2>Paper equity (cumulative P&amp;L, settled trades)</h2><div id="curve" class="muted">no settled trades yet</div></div>
<div class="card" style="margin-bottom:12px"><h2>Daily paper results (UTC)</h2><table id="daily"></table></div>
<div class="card" style="margin-bottom:12px"><h2>Open trades · settlement ETA (UTC)</h2><table id="open"></table></div>
<div class="card" style="margin-bottom:12px"><h2>Paper trades (last 50)</h2><table id="trades"></table></div>
<div class="card" style="margin-bottom:12px"><h2>Recent engine events</h2><table id="recent"></table></div>

<h3 class="section">System</h3>
<div class="grid">
  <div class="card"><h2>Feeds</h2><div id="feeds"></div></div>
  <div class="card"><h2>Latency</h2><div id="latency"></div></div>
  <div class="card"><h2>Execution</h2><div id="execution"></div></div>
  <div class="card"><h2>Data quality</h2><div id="dataq"></div></div>
</div>
</main>
<script>
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "–").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const f = (v, d = 2) => (v === null || v === undefined) ? "–" : Number(v).toFixed(d);
const kv = (rows) => rows.map(([k, v, cls]) => `<div class="kv"><span>${esc(k)}</span><span class="${cls || ""}">${esc(v)}</span></div>`).join("");
const verdict = (v) => v === null || v === undefined ? ["pending", "muted"] : v ? ["pass", "good"] : ["FAIL", "bad"];
const mid = (q) => q && q.valid && q.bid !== null && q.ask !== null ? (q.bid + q.ask) / 2 : null;
// Write an element only when its content changed: no flicker, and hovers,
// selections and scroll positions survive every refresh.
const LAST = new Map();
function put(id, html) {
  if (LAST.get(id) === html) return;
  LAST.set(id, html);
  const el = $(id); if (el) el.innerHTML = html;
}
// Ages tick every second from the last fetch, instead of jumping each poll.
const FETCHED = { summary: null, state: null };
const FAILS = { summary: 0, state: 0, series: 0 };
function ages() {
  const now = Date.now() / 1000, a = (t) => t == null ? null : Math.max(0, Math.round(now - t));
  const s = a(FETCHED.summary), st = a(FETCHED.state);
  put("age", s == null ? "" : `<span class="${s > 30 ? "bad" : "muted"}">data ${s}s old</span>`);
  put("state-age", st == null ? "" : `<span class="${st > 15 ? "bad" : "muted"}">as of ${st}s ago</span>`);
  const down = Object.values(FAILS).some((n) => n >= 2);
  put("conn", down ? '<span class="badge">reconnecting…</span>' : "");
}
// Poll on a schedule, skip while the tab is hidden, catch up the moment it is shown.
const JOBS = [];
function every(fn, ms) { JOBS.push(fn); fn(); setInterval(() => { if (!document.hidden) fn(); }, ms); }
document.addEventListener("visibilitychange", () => { if (!document.hidden) JOBS.forEach((fn) => fn()); });

async function tick() {
  let body;
  try { const r = await fetch("api/summary"); if (!r.ok) throw new Error(r.status); body = await r.json(); FAILS.summary = 0; }
  catch (e) { FAILS.summary++; ages(); return; }  // keep showing the last good data
  const live = body.live, g = body.gates || {}, res = body.results || {};
  FETCHED.summary = body.fetched_at;
  const dataAge = body.fetched_at ? Math.round(Date.now() / 1000 - body.fetched_at) : null;
  put("sub", live
    ? `<b>${esc(live.mode)}</b> · ${esc(live.host)} · gap ≥ ${f(live.params.gap_threshold)} · cap ${f(live.params.max_entry_px)} · $${f(live.params.notional_usd, 0)} · ${live.risk && live.risk.halted ? '<span class="bad">HALTED</span>' : '<span class="good">running</span>'} · <span class="muted" title="results epoch (PK_RESULTS_EPOCH) and code build; results shown are this epoch only">epoch ${esc(live.epoch)} · ${esc((live.build || "").slice(0, 8))}${body.excluded_records ? ` · ${body.excluded_records} older records excluded` : ""}</span>`
    : '<span class="bad">service unreachable</span>');
  const money = (v) => v == null ? "–" : (v < 0 ? "−$" : "$") + f(Math.abs(v));
  const L = (live && live.ledger) || {}, cap = L.start_cash, dd = res.drawdown || {};
  const pctOf = (v) => cap && v != null ? f((100 * v) / cap, 2) + "%" : "–";
  const sign = (v) => v == null || Math.abs(v) < 0.005 ? "" : v < 0 ? "bad" : "good";
  const equity = L.equity_at_cost;
  put("capital", kv([["initial capital", money(cap)], ["cash", money(L.cash)], ["equity (open at cost)", money(equity)],
    ["realised P&L · return", `${money(L.realised_total)} · ${pctOf(L.realised_total)}`, sign(L.realised_total)],
    ["equity return", cap && equity != null ? pctOf(equity - cap) : "–", cap && equity != null ? sign(equity - cap) : ""],
    ["realised today", money(L.realised_today), sign(L.realised_today)],
    ["open positions · capital in use", `${L.open_positions ?? 0} · ${equity ? f((100 * (L.open_notional || 0)) / equity, 1) + "%" : "–"}`],
    ["largest open exposure", money(L.largest_open_cost)],
    ["max drawdown", `${money(dd.max)} · ${pctOf(dd.max)}`, dd.max > 0 ? "bad" : ""], ["current drawdown", `${money(dd.current)} · ${pctOf(dd.current)}`, dd.current > 0 ? "warn" : ""]]));
  const Q = res.quality || {};
  put("quality", kv([["trades · wins · losses", `${Q.n ?? 0} · ${Q.wins ?? 0} · ${Q.losses ?? 0}`],
    ["avg win · avg loss", `${money(Q.avg_win)} · ${money(Q.avg_loss)}`], ["profit factor", f(Q.profit_factor), Q.profit_factor == null ? "" : Q.profit_factor >= 1 ? "good" : "bad"],
    ["expectancy / trade", money(Q.expectancy), sign(Q.expectancy)], ["largest win · loss", `${money(Q.largest_win)} · ${money(Q.largest_loss)}`],
    ["longest losing streak", Q.longest_losing_streak ?? 0], ["gross P&L (before fees)", money(Q.gross_pnl), sign(Q.gross_pnl)],
    ["fees · % of gross", `${money(Q.fees)} · ${Q.fees_pct_of_gross == null ? "–" : f(Q.fees_pct_of_gross, 1) + "%"}`]]));
  const FN = res.funnel || {}, today = (res.daily || [])[0] || {};
  // Numbers only, so the "(today)" span can be built as HTML without escaping.
  const tdy = (v) => ` <span class="muted">(${Number(v ?? 0)})</span>`;
  const kvRaw = (rows) => rows.map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><span>${v}</span></div>`).join("");
  put("funnel", kvRaw([["windows seen", Number(FN.windows ?? 0) + tdy(today.windows)], ["crossings (|Δ| ≥ threshold)", Number(FN.crossings ?? 0) + tdy((today.enter || 0) + (today.skip || 0) + (today.blocked || 0))],
    ["skipped (above cap)", Number(FN.skipped_above_cap ?? 0) + tdy(today.skip)], ["blocked (risk / mode)", Number(FN.blocked ?? 0) + tdy(today.blocked)],
    ["orders", Number(FN.orders ?? 0) + tdy((today.filled || 0) + (today.no_fill || 0))], ["filled", Number(FN.filled ?? 0) + tdy(today.filled)], ["settled", Number(FN.settled ?? 0) + tdy(today.settled)]]));
  const cnt = (live && live.counters) || {}, DQ = res.data_quality || {}, fk = ((live && live.feeds) || {}).kalshi || {};
  const sumBy = (pred) => Object.entries(cnt).filter(([k]) => pred(k)).reduce((t, [, v]) => t + v, 0);
  const assetsNow = Object.values((live && live.assets) || {});
  const crossedNow = assetsNow.filter((a) => Object.values(a.legs || {}).some((l) => l && l.state === "crossed")).length;
  put("dataq", kv([["legs crossed now", crossedNow, crossedNow ? "bad" : ""], ["crossed (evaluations)", sumBy((k) => k.endsWith("_crossed")), sumBy((k) => k.endsWith("_crossed")) ? "bad" : ""],
    ["one-sided · not updated · no snapshot", `${sumBy((k) => k.endsWith("_one_sided"))} · ${sumBy((k) => k.endsWith("_not_updated"))} · ${sumBy((k) => k.endsWith("_no_snapshot"))}`],
    ["invalid-leg share of evaluations", DQ.invalid_leg_share == null ? "–" : f(DQ.invalid_leg_share * 100, 2) + "%"],
    ["Kalshi seq gaps · reconnects", `${fk.seq_gaps ?? "–"} · ${fk.connects ?? "–"}`], ["Polymarket reconnects", (((live && live.feeds) || {}).poly || {}).connects ?? "–"],
    ["unidentified windows", (DQ.unidentified_windows ?? 0) + (cnt.windows_unidentified ? ` · ${cnt.windows_unidentified} live` : ""), DQ.unidentified_windows ? "warn" : ""],
    ["engine errors", cnt.engine_errors ?? 0, cnt.engine_errors ? "bad" : ""],
    ["awaiting settlement · oldest", `${L.open_positions ?? 0} · ${L.oldest_open_age_s == null ? "–" : f(L.oldest_open_age_s, 0) + "s"}`, L.oldest_open_age_s > 600 ? "warn" : ""]]));
  const issues = [];
  if (!live) issues.push("service unreachable");
  else {
    for (const [name, key] of [["Kalshi", "kalshi"], ["Polymarket", "poly"]]) {
      const fd = (live.feeds || {})[key] || {}, ag = (live.feed_age_s || {})[key === "kalshi" ? "k" : "p"];
      if (!fd.connected) issues.push(`${name} feed down`); else if (ag > 10) issues.push(`${name} feed stale (${f(ag, 0)}s)`);
    }
    if (live.risk && live.risk.halted) issues.push("HALTED: new paper entries blocked");
    if (live.risk && live.risk.kill_file_present) issues.push("kill file present");
    if (crossedNow) issues.push(`${crossedNow} asset(s) with a crossed book`);
    const LEG = { kalshi: "Kalshi", poly_yes: "Poly YES", poly_no: "Poly NO" };
    for (const [name, a] of Object.entries(live.assets || {})) {
      if (!a.identified || a.sec_into == null || a.sec_into < 20) continue;  // books arrive after the roll
      for (const [leg, l] of Object.entries(a.legs || {})) {
        if (l && ["no_snapshot", "no_book", "not_updated"].includes(l.state)) issues.push(`${name}: ${LEG[leg] || leg} ${WHY[l.state] || l.state}`);
      }
    }
    const unid = assetsNow.filter((a) => !a.identified).length; if (unid) issues.push(`${unid} asset(s) without an identified window`);
    if (L.rejects) issues.push(`${L.rejects} balance reject(s)`);
    if (live.recorder && live.recorder.disk_paused) issues.push("recorder paused: disk low");
    if (dataAge > 30) issues.push(`data ${dataAge}s old`);
    if (live.telegram && live.telegram.conflicts) issues.push("Telegram /kill not reaching this service: another process polls the same bot token");
    const hostLoad = live.host_load || {};
    if (hostLoad.cpu_pct > 85) issues.push(`trader CPU at ${f(hostLoad.cpu_pct, 0)}%: the host may be too small for the feeds`);
  }
  $("alerts").className = "alerts " + (issues.length ? "bad" : "ok");
  put("alerts", issues.length ? "<ul>" + issues.map((i) => `<li>${esc(i)}</li>`).join("") + "</ul>" : "✓ all clear: feeds up, books sane, no halts or rejects");
  const feeds = (live && live.feeds) || {}, age = (live && live.feed_age_s) || {};
  const hl = (live && live.host_load) || {};
  put("feeds", kv([["Kalshi", (feeds.kalshi && feeds.kalshi.connected ? "up" : "DOWN") + ` · ${f(age.k, 1)}s`, feeds.kalshi && feeds.kalshi.connected ? "good" : "bad"],
    ["Polymarket", (feeds.poly && feeds.poly.connected ? "up" : "DOWN") + ` · ${f(age.p, 1)}s`, feeds.poly && feeds.poly.connected ? "good" : "bad"],
    ["Kalshi seq gaps", feeds.kalshi ? feeds.kalshi.seq_gaps : "–"], ["recorder lines", live && live.recorder ? live.recorder.lines_written : "–"],
    ["disk free", live && live.recorder ? f(live.recorder.disk_free_gb, 1) + " GB" : "–"],
    ["process CPU · load (cores)", hl.cpu_pct == null ? "–" : `${f(hl.cpu_pct, 0)}% · ${f(hl.load1, 2)} (${hl.cpus ?? "–"})`, hl.cpu_pct > 85 ? "bad" : hl.cpu_pct > 60 ? "warn" : ""],
    ["slow-consumer drops (Poly · Kalshi)", `${(feeds.poly || {}).slow_consumer ?? 0} · ${(feeds.kalshi || {}).slow_consumer ?? 0}`, ((feeds.poly || {}).slow_consumer || (feeds.kalshi || {}).slow_consumer) ? "warn" : ""]]));
  const gg = g.gates || {}, v = g.vwap || {};
  put("gates", kv([["entries (≤ cap)", `${g.in_band_n ?? 0} / ${g.gate_min_n ?? 300}`], ["avg / p90 fill", `${f(v.avg, 3)} / ${f(v.p90, 3)}`, verdict(gg.fill_price)[1]],
    ["fill rate", f((g.fill_rate ?? 0) * 100, 1) + "%"], ["net ¢/share", f(g.net_cents_per_share)], ["$ / entry", f(g.dollars_per_entry)],
    ["day-t (" + (g.days ?? 0) + "d, " + (g.days_positive ?? 0) + " +)", f(g.day_t), verdict(gg.day_t)[1]], ["venue disagreement", g.venue_disagreement == null ? "–" : f(g.venue_disagreement * 100, 1) + "%"]]));
  const lat = (live && live.latency) || {}, rates = body.rates || {};
  const pq = (m, unit, d = 0) => lat[m] ? `${f(lat[m].p50, d)} / ${f(lat[m].p99, d)} ${unit}` : "–";
  put("latency", kv([["Polymarket feed p50 / p99", pq("feed_ms_p", "ms"), (lat.feed_ms_p || {}).p99 > 1000 ? "bad" : ""],
    ["Kalshi feed p50 / p99", pq("feed_ms_k", "ms"), (lat.feed_ms_k || {}).p99 > 1000 ? "bad" : ""],
    ["messages / s (Poly · Kalshi)", `${f(rates.poly, 0)} · ${f(rates.kalshi, 0)}`],
    ["engine per message p50 / p99", pq("engine_us", "µs")], ["event-loop lag p50 / p99", pq("loop_lag_ms", "ms", 1), (lat.loop_lag_ms || {}).p99 > 100 ? "bad" : ""],
    ["Kalshi book age at decision", pq("age_ms_kalshi", "ms")], ["Poly book age at decision", pq("age_ms_poly", "ms")],
    ["result delay Poly / Kalshi (p50)", `${f((lat.resolution_s_poly || {}).p50, 0)} / ${f((lat.resolution_s_kalshi || {}).p50, 0)} s`]]));
  const S = res.signal || {};
  const sigCls = S.n >= 30 && S.win_rate != null && S.avg_price != null ? (S.win_rate > S.avg_price ? "good" : "bad") : "";
  put("signal", kv([["settled trades", S.n ?? 0], ["win rate", S.win_rate == null ? "–" : f(S.win_rate * 100, 1) + "%", sigCls],
    ["Kalshi-implied prob. of our leg", S.kalshi_implied == null ? "–" : f(S.kalshi_implied * 100, 1) + "%"],
    ["average price paid", S.avg_price == null ? "–" : f(S.avg_price * 100, 1) + "¢"], ["net ¢/share", f(S.net_cents)],
    ["reading", S.n >= 30 ? (S.win_rate > S.avg_price + 0.02 ? "signal informative" : S.win_rate < S.avg_price - 0.05 ? "below price: check for a bug" : "≈ price: no edge yet") : "need ≥ 30 settled", sigCls]]));
  const E = res.execution || {};
  put("execution", kv([["orders / filled", `${E.orders ?? 0} / ${E.filled ?? 0}`], ["fill rate", E.fill_rate == null ? "–" : f(E.fill_rate * 100, 1) + "%"],
    ["avg share of order filled", E.avg_fill_ratio == null ? "–" : f(E.avg_fill_ratio * 100, 1) + "%"], ["avg slippage vs touch", f(E.avg_slippage_cents) + "¢"],
    ["avg fee / share", f(E.avg_fee_cents_per_share) + "¢"], ["order latency p50 (paper)", f(E.order_latency_ms_p50, 0) + " ms"],
    ["Kalshi age at decision p50 / p90", `${f(E.kalshi_age_ms_p50, 0)} / ${f(E.kalshi_age_ms_p90, 0)} ms`]]));
  const grp = (rows) => "<tr><th></th><th>n</th><th>win %</th><th>Kalshi %</th><th>paid ¢</th><th>net ¢/sh</th><th>P&amp;L</th></tr>" +
    (Object.keys(rows).length ? Object.entries(rows).map(([k, r]) => `<tr><td>${esc(k)}</td><td>${r.n}</td><td>${r.win_rate == null ? "–" : f(r.win_rate * 100, 1)}</td>
      <td>${r.kalshi_implied == null ? "–" : f(r.kalshi_implied * 100, 1)}</td><td>${r.avg_price == null ? "–" : f(r.avg_price * 100, 1)}</td>
      <td class="${r.net_cents == null ? "" : r.net_cents < 0 ? "bad" : "good"}">${f(r.net_cents)}</td><td class="${r.pnl < 0 ? "bad" : "good"}">${f(r.pnl)}</td></tr>`).join("") : '<tr><td class="muted">no settled trades yet</td></tr>');
  put("by-asset", grp(res.by_asset || {}));
  put("by-sec", grp(res.by_sec || {}));
  const C = res.curve || [];
  if (C.length > 1) {
    const W = 1000, H = 140, P = 6, vals = C.map((c) => c[1]), lo = Math.min(0, ...vals), hi = Math.max(0, ...vals), span = hi - lo || 1;
    const x = (i) => P + (i / (C.length - 1)) * (W - 2 * P), y = (v) => H - P - ((v - lo) / span) * (H - 2 * P);
    const pts = C.map((c, i) => `${x(i).toFixed(1)},${y(c[1]).toFixed(1)}`).join(" "), last = vals[vals.length - 1];
    put("curve", `<svg class="curve" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="cumulative paper P&L">
      <line class="zero" x1="0" x2="${W}" y1="${y(0)}" y2="${y(0)}"/><polyline class="line" points="${pts}" stroke="${last < 0 ? "var(--bad)" : "var(--good)"}"/></svg>
      <div class="kv"><span>${C.length} settled · since ${new Date(C[0][0] * 1000).toISOString().slice(0, 16).replace("T", " ")}</span><span class="${last < 0 ? "bad" : "good"}">$${f(last)}</span></div>`);
  }
  const D = res.daily || [];
  put("daily", "<tr><th>day</th><th>windows</th><th>entries</th><th>skips</th><th>blocked</th><th>filled</th><th>no fill</th><th>avg fill</th><th>win %</th><th>net ¢/sh</th><th>fees</th><th>P&amp;L</th><th>cum.</th></tr>" +
    (D.length ? D.map((d) => `<tr><td>${esc(d.day)}</td><td>${d.windows ?? 0}</td><td>${d.enter}</td><td>${d.skip}</td><td class="${d.blocked ? "warn" : ""}">${d.blocked}</td><td>${d.filled}</td><td>${d.no_fill}</td>
      <td class="${d.avg_vwap > 0.6 ? "bad" : ""}">${f(d.avg_vwap, 3)}</td><td>${d.win_rate == null ? "–" : f(d.win_rate * 100, 1)}</td><td>${f(d.net_cents)}</td><td>${f(d.fees)}</td>
      <td class="${d.pnl < 0 ? "bad" : "good"}">${f(d.pnl)}</td><td class="${d.cumulative < 0 ? "bad" : "good"}">${f(d.cumulative)}</td></tr>`).join("") : '<tr><td class="muted">no decisions yet</td></tr>'));
  const OT = body.open_trades || {}, OTR = OT.trades || [], nowS = Date.now() / 1000;
  const clock = (u) => new Date(u * 1000).toISOString().slice(11, 19);
  const cd = (u) => { const s = Math.round(u - nowS); return s < 0 ? "due" : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  const basis = OT.delay_samples ? `median ${f(OT.settle_delay_s, 0)}s over ${OT.delay_samples} Polymarket resolutions` : `default ${f(OT.settle_delay_s, 0)}s (no measured resolution yet)`;
  put("open", "<tr><th>asset</th><th>side</th><th>shares</th><th>paid</th><th>window closes</th><th>settles ~</th><th>in</th><th>Kalshi hint</th></tr>" +
    (OTR.length ? OTR.map((t) => `<tr><td><b>${esc(t.asset)}</b></td><td>${esc(t.side)}</td><td>${f(t.shares, 0)}</td><td>$${f(t.cost)}</td>
      <td>${clock(t.close_unix)}</td><td>${clock(t.eta_unix)}</td><td>${cd(t.eta_unix)}</td><td>${esc(t.kalshi_outcome ?? "–")}</td></tr>`).join("")
      : '<tr><td class="muted" colspan="8">no open trades</td></tr>') +
    `<tr><td colspan="8" class="muted" style="text-align:left;border-bottom:none">ETA = window close + ${esc(basis)}; Kalshi hint is informational only, venues can disagree.</td></tr>`);
  const T = res.trades || [];
  put("trades", "<tr><th>time (UTC)</th><th>asset</th><th>side</th><th>gap</th><th>sec</th><th>touch</th><th>fill</th><th>shares</th><th>fee</th><th>outcome</th><th>P&amp;L</th></tr>" +
    (T.length ? T.map((t) => `<tr><td>${new Date(t.t_ns / 1e6).toISOString().slice(5, 19).replace("T", " ")}</td><td>${esc(t.asset)}</td><td>${esc(t.side)}</td><td>${f(t.gap, 3)}</td><td>${f(t.sec_into, 0)}</td>
      <td>${f(t.touch_px, 2)}</td><td>${f(t.vwap, 3)}</td><td>${f(t.shares, 0)}/${f(t.ordered, 0)}</td><td>${f(t.fee)}</td><td>${esc(t.outcome ?? "open")}</td>
      <td class="${t.pnl == null ? "muted" : t.pnl < 0 ? "bad" : "good"}">${t.pnl == null ? "–" : f(t.pnl)}</td></tr>`).join("") : '<tr><td class="muted">no fills yet</td></tr>'));
  const B = g.bands || {};
  put("bands", "<tr><th>entry band</th><th>n</th><th>net ¢/share</th><th>manual excess ¢</th></tr>" +
    Object.entries(B).map(([b, x], i) => `<tr><td>${esc(b)}</td><td>${x.n}</td><td class="${x.net_cents == null ? "" : x.net_cents < 0 ? "bad" : "good"}">${f(x.net_cents)}</td><td class="muted">${[16.2, 14.9, 12.6, 8.3, 1.0, 1.6, -0.4, -0.6][i]}</td></tr>`).join(""));
  const R = (live && live.recent) || [];
  put("recent", "<tr><th>type</th><th>asset</th><th>window</th><th>action / side</th><th>px</th><th>shares</th><th>gap</th><th>P&amp;L</th></tr>" +
    R.map((r) => `<tr><td>${esc(r.type)}</td><td>${esc(r.asset)}</td><td>${r.open_unix ? new Date(r.open_unix * 1000).toISOString().slice(5, 16).replace("T", " ") : "–"}</td>
      <td>${esc(r.type === "decision" ? r.action + " " + r.side + (r.blocked ? " (" + r.blocked + ")" : "") : r.side)}</td><td>${f(r.vwap ?? r.entry_px, 3)}</td>
      <td>${f(r.shares ?? r.contracts, 0)}</td><td>${f(r.gap, 3)}</td><td class="${r.pnl == null ? "" : r.pnl < 0 ? "bad" : "good"}">${f(r.pnl)}</td></tr>`).join(""));
}
function phase(sec, early, late) {
  if (sec == null) return ["–", "muted"];
  if (sec < early) return [`${f(sec, 0)}s · early`, "muted"];
  if (sec > 900 - late) return [`${f(sec, 0)}s · late`, "muted"];
  return [`${f(sec, 0)}s · open`, ""];
}
function dbar(gap, thr) {
  const W = 0.5, pos = (v) => 50 + (Math.max(-W, Math.min(W, v)) / W) * 50;
  let fill = "";
  if (gap != null) {
    const a = pos(Math.min(0, gap)), b = pos(Math.max(0, gap));
    fill = `<i style="left:${a}%;width:${Math.max(b - a, 0.5)}%;background:${Math.abs(gap) >= thr ? "var(--good)" : "var(--muted)"}"></i>`;
  }
  return `<span class="dbar"><i class="mid" style="left:50%"></i>${fill}<b style="left:${pos(thr)}%"></b><b style="left:${pos(-thr)}%"></b></span>`;
}
const px = (v) => v == null ? "–" : String(Number(v.toFixed(3)));
const WHY = { one_sided: "one-sided", crossed: "CROSSED", not_updated: "no update this window", no_snapshot: "no snapshot", no_book: "not subscribed" };
// A valid leg shows bid–ask (mid). An invalid one shows why, with the side(s) it has:
// near expiry a book at 0.99 is legitimately one-sided; "crossed" is a fault.
const ba = (q, leg, showMid = true) => {
  if (q && q.valid) return `${px(q.bid)}–${px(q.ask)}` + (showMid ? ` <span class="muted">(${px((q.bid + q.ask) / 2)})</span>` : "");
  const L = leg || {}, why = WHY[L.state] || "invalid";
  if (L.state === "one_sided") {
    // Normal near expiry: the outcome is all but decided, so one side empties.
    const text = L.bid != null ? `${px(L.bid)} · no ask` : L.ask != null ? `no bid · ${px(L.ask)}` : "empty book";
    return `<span class="muted" title="one-sided book: normal when the outcome is all but decided">${text}</span>`;
  }
  const sides = L.bid != null || L.ask != null ? `${L.bid == null ? "no bid" : px(L.bid)} / ${L.ask == null ? "no ask" : px(L.ask)} · ` : "";
  return `<span class="bad" title="leg invalid: ${esc(why)}">${sides}${esc(why)}</span>`;
};
const REASON = { too_late: "too late", too_early: "too early", below_threshold: "below threshold", invalid_leg: "invalid leg", traded: "window used" };
const REASON_TIP = { too_late: "no new entries in the last 60 s of the window", too_early: "no entries in the first 20 s of the window" };
const ms = (v) => v == null ? "–" : v >= 10000 ? f(v / 1000, 0) + "s" : f(v, 0);
async function stateTick() {
  let body;
  try { const r = await fetch("api/state"); if (!r.ok) throw new Error(r.status); body = await r.json(); FAILS.state = 0; }
  catch (e) { FAILS.state++; ages(); return; }
  FETCHED.state = body.fetched_at;
  const p = body.params || {}, thr = p.gap_threshold ?? 0.2, early = p.entry_early_s ?? 20, late = p.entry_late_s ?? 60;
  ages();
  const rows = Object.entries(body.assets || {});
  put("mkstate", '<colgroup><col style="width:4.5%"><col style="width:7%"><col style="width:13%"><col style="width:13%"><col style="width:11%"><col style="width:5.5%"><col style="width:6.5%"><col style="width:5.5%"><col style="width:9%"><col style="width:8%"><col style="width:5%"><col style="width:12%"></colgroup>' + "<tr><th>asset</th><th>window</th><th title='bid–ask (mid)'>Kalshi YES</th><th title='bid–ask (mid)'>Poly YES</th><th title='bid–ask'>Poly NO</th><th>Δ</th><th></th><th>to thr.</th><th>rule now</th><th>age K·Y·N ms</th><th>max |Δ|</th><th>this window</th></tr>" +
    (rows.length ? rows.map(([n, a]) => {
      if (!a.identified) return `<tr><td>${esc(n)}</td><td colspan="11" class="bad">window not identified</td></tr>`;
      const [ph, phc] = phase(a.sec_into, early, late), g = a.gap, pv = a.preview || {}, d = a.decision, ag = a.age_ms || {};
      const to = g == null ? "–" : Math.abs(g) >= thr ? '<span class="good">crossed</span>' : f(thr - Math.abs(g), 3);
      const rule = pv.action === "enter" ? `<span class="pill enter">enter ${esc(pv.side)} ${f(pv.entry_px, 2)}</span>`
        : pv.action === "skip" ? `<span class="pill skip" title="above the entry-price cap">skip ${esc(pv.side)} ${f(pv.entry_px, 2)}</span>`
        : `<span class="muted" title="${esc(REASON_TIP[pv.reason] || "")}">${esc(REASON[pv.reason] || (pv.reason || "–").replace(/_/g, " "))}</span>`;
      const done = d ? `<span class="pill ${esc(d.action)}">${esc(d.action)} ${esc(d.side)} ${f(d.entry_px, 2)} @${f(d.sec_into, 0)}s${d.blocked ? " · " + esc(d.blocked) : ""}</span>` : '<span class="muted">none yet</span>';
      const stale = (v) => v != null && v > 5000 ? "bad" : "";
      return `<tr><td><b>${esc(n)}</b></td><td class="${phc}">${ph}</td><td class="num">${ba(a.kalshi, (a.legs || {}).kalshi)}</td>
        <td class="num">${ba(a.poly_yes, (a.legs || {}).poly_yes)}</td><td class="num">${ba(a.poly_no, (a.legs || {}).poly_no, false)}</td>
        <td class="num ${g == null ? "" : Math.abs(g) >= thr ? "good" : ""}"><b>${g == null ? "–" : (g >= 0 ? "+" : "") + f(g, 3)}</b></td><td>${dbar(g, thr)}</td><td class="num">${to}</td><td>${rule}</td>
        <td class="num"><span class="${stale(ag.kalshi)}">${ms(ag.kalshi)}</span> · <span class="${stale(ag.poly_yes)}">${ms(ag.poly_yes)}</span> · <span class="${stale(ag.poly_no)}">${ms(ag.poly_no)}</span></td>
        <td class="num">${f(a.max_abs_gap, 3)}</td><td>${done}</td></tr>`;
    }).join("") : '<tr><td class="muted">no assets</td></tr>'));
}
const SERIES = {}, MAX_POINTS = 1800;
let seriesLast = 0, seriesMeta = {}, view = location.hash === "#prev" ? "prev" : "cur";
const W = 520, PT = 8, PH = 170, GT = 196, GH = 70, X0 = 36, X1 = W - 8, H = GT + GH + 20;
const sx = (sec) => X0 + (sec / 900) * (X1 - X0);
const sy = (p) => PT + (1 - p) * PH;
const GMAX = 0.5, gy = (g) => GT + (1 - (Math.max(-GMAX, Math.min(GMAX, g)) + GMAX) / (2 * GMAX)) * GH;
function lines(points, xf, yf, cls) {
  // One polyline per run of valid values: a gap in the data stays a gap on the chart.
  const runs = []; let run = [];
  for (const [x, v] of points) { if (v === null || v === undefined) { if (run.length) runs.push(run); run = []; } else run.push(`${xf(x).toFixed(1)},${yf(v).toFixed(1)}`); }
  if (run.length) runs.push(run);
  return runs.filter((r) => r.length > 1).map((r) => `<polyline class="${cls}" points="${r.join(" ")}"/>`).join("");
}
function chart(asset, pts, open, decisions) {
  const thr = seriesMeta.threshold ?? 0.2, early = seriesMeta.entry_early_s ?? 20, late = seriesMeta.entry_late_s ?? 60;
  const rows = pts.map((p) => ({ s: p[0] - open, k: p[2], y: p[3], g: p[2] !== null && p[3] !== null ? p[2] - p[3] : null }));
  const last = rows[rows.length - 1] || {};
  let svg = `<rect class="shade" x="${sx(0)}" y="${PT}" width="${sx(early) - sx(0)}" height="${GT + GH - PT}"/><rect class="shade" x="${sx(900 - late)}" y="${PT}" width="${sx(900) - sx(900 - late)}" height="${GT + GH - PT}"/>`;
  for (const p of [0, 0.5, 1]) svg += `<line class="grid" x1="${X0}" x2="${X1}" y1="${sy(p)}" y2="${sy(p)}"/><text x="${X0 - 4}" y="${sy(p) + 3}" text-anchor="end">${p}</text>`;
  svg += `<line class="grid" x1="${X0}" x2="${X1}" y1="${gy(0)}" y2="${gy(0)}"/><line class="thr" x1="${X0}" x2="${X1}" y1="${gy(thr)}" y2="${gy(thr)}"/><line class="thr" x1="${X0}" x2="${X1}" y1="${gy(-thr)}" y2="${gy(-thr)}"/>`;
  svg += `<text x="${X0 - 4}" y="${gy(thr) + 3}" text-anchor="end">+${thr}</text><text x="${X0 - 4}" y="${gy(-thr) + 3}" text-anchor="end">−${thr}</text>`;
  for (const t of [0, 300, 600, 900]) svg += `<text x="${sx(t)}" y="${H - 4}" text-anchor="middle">${t}s</text>`;
  svg += lines(rows.map((r) => [r.s, r.k]), sx, sy, "k") + lines(rows.map((r) => [r.s, r.y]), sx, sy, "p") + lines(rows.map((r) => [r.s, r.g]), sx, gy, "g");
  let note = "";
  for (const d of decisions.filter((d) => d.asset === asset && d.open_unix === open)) {
    const x = sx(d.sec_into), color = d.action === "enter" ? (d.blocked ? "var(--warn)" : "var(--good)") : "var(--muted)";
    svg += `<line x1="${x}" x2="${x}" y1="${PT}" y2="${GT + GH}" stroke="${color}" stroke-width="1.5"/>`;
    if (d.entry_px != null) { const ey = sy(d.side === "yes" ? d.entry_px : 1 - d.entry_px); svg += `<circle cx="${x}" cy="${ey}" r="3.5" fill="${color}"/>`; }
    note = `${d.action} ${d.side} @ ${f(d.entry_px, 2)}${d.blocked ? " (" + d.blocked + ")" : ""} · ${f(d.sec_into, 0)}s`;
  }
  const head = rows.length ? `${f(last.s, 0)}s · K ${f(last.k, 2)} · P ${f(last.y, 2)} · gap ${last.g == null ? "–" : (last.g >= 0 ? "+" : "") + f(last.g, 2)}` : "no data";
  const start = new Date(open * 1000).toISOString().slice(11, 16);
  return `<div class="chart"><h3>${esc(asset)} <span>${start} UTC · ${head}</span></h3>
    <svg class="mkt" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(asset)} Kalshi and Polymarket YES mids and their gap, window opening ${start} UTC">${svg}</svg>
    <div class="muted" style="font-size:12px">${note ? esc(note) : "no decision in this window"}</div></div>`;
}
function renderCharts(decisions) {
  const html = Object.keys(SERIES).sort().map((asset) => {
    const pts = SERIES[asset]; if (!pts.length) return "";
    const opens = [...new Set(pts.map((p) => p[1]))].sort((a, b) => b - a);
    const open = view === "cur" ? opens[0] : opens[1];
    if (open === undefined) return `<div class="chart"><h3>${esc(asset)} <span>no previous window recorded yet</span></h3></div>`;
    return chart(asset, pts.filter((p) => p[1] === open), open, decisions);
  }).join("");
  if (html) put("charts", html);
}
let lastDecisions = [];
async function seriesTick() {
  let body;
  try { const r = await fetch(`api/series?since=${seriesLast}`); if (!r.ok) throw new Error(r.status); body = await r.json(); FAILS.series = 0; }
  catch (e) { FAILS.series++; ages(); return; }
  seriesMeta = body; lastDecisions = body.decisions || [];
  for (const [asset, pts] of Object.entries(body.points || {})) {
    const arr = (SERIES[asset] ||= []);
    for (const p of pts) { if (p[0] > seriesLast) seriesLast = Math.max(seriesLast, p[0]); arr.push(p); }
    if (arr.length > MAX_POINTS) arr.splice(0, arr.length - MAX_POINTS);
  }
  renderCharts(lastDecisions);
}
for (const [id, v] of [["v-cur", "cur"], ["v-prev", "prev"]]) {
  $(id).addEventListener("click", () => { view = v; history.replaceState(null, "", v === "prev" ? "#prev" : "#"); pressed(); renderCharts(lastDecisions); });
}
function pressed() { $("v-cur").setAttribute("aria-pressed", view === "cur"); $("v-prev").setAttribute("aria-pressed", view === "prev"); }
pressed();
every(tick, 10000);
every(seriesTick, 5000);
every(stateTick, 5000);
setInterval(ages, 1000);
</script>
</body>
</html>
"""
