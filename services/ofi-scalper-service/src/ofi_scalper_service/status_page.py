"""The status page: one self-contained document, no external requests.

It reads ``/api/summary`` every 30 s and ``/api/history`` every minute, and
draws its charts as inline SVG. Light and dark follow the viewer's system.
"""

from __future__ import annotations

__all__ = ["STATUS_HTML"]

STATUS_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>OFI Scalper Status</title>
<style>
:root {
  --bg: #f6f6f3; --card: #ffffff; --fg: #1c1c1a; --muted: #6a6a64; --line: #e3e3dd;
  --accent: #2f5fd0; --good: #1b7a3d; --bad: #b3261e; --warn: #8a5a00; --bar: #c9d6f5;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #141413; --card: #1d1d1b; --fg: #ececea; --muted: #9a9a93; --line: #2f2f2c;
    --accent: #8fb0ff; --good: #5cc27f; --bad: #ff8a80; --warn: #e0b050; --bar: #34436b;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
       font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
header { padding: 20px 16px 8px; max-width: 1200px; margin: 0 auto; }
header h1 { margin: 0; font-size: 20px; }
header p { margin: 4px 0 0; color: var(--muted); }
nav { position: sticky; top: 0; z-index: 2; background: var(--bg); border-bottom: 1px solid var(--line); }
nav div { max-width: 1200px; margin: 0 auto; padding: 0 16px; display: flex; gap: 4px; overflow-x: auto; }
nav a { padding: 10px 12px; color: var(--muted); text-decoration: none; white-space: nowrap;
        border-bottom: 2px solid transparent; }
nav a.on { color: var(--fg); border-bottom-color: var(--accent); }
main { max-width: 1200px; margin: 0 auto; padding: 16px; }
section { display: none; }
section.on { display: block; }
h2 { font-size: 15px; margin: 24px 0 8px; }
h2:first-child { margin-top: 0; }
.grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px;
        min-width: 0; }
.card h3 { margin: 0 0 6px; font-size: 13px; color: var(--muted); font-weight: 600; }
.big { font-size: 22px; font-weight: 650; }
.muted { color: var(--muted); }
.good { color: var(--good); } .bad { color: var(--bad); } .warn { color: var(--warn); }
.chip { display: inline-block; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--line);
        margin: 2px 4px 2px 0; font-size: 12px; }
.chip.good { border-color: var(--good); } .chip.bad { border-color: var(--bad); }
.bar { height: 8px; background: var(--line); border-radius: 4px; overflow: hidden; margin: 8px 0 4px; }
.bar > div { height: 100%; background: var(--accent); }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); white-space: nowrap; }
th { color: var(--muted); font-weight: 500; font-size: 12px; }
ol.road { list-style: none; padding: 0; margin: 0; }
ol.road li { display: grid; grid-template-columns: 22px 1fr; gap: 8px; padding: 6px 0;
             border-bottom: 1px solid var(--line); }
ol.road .dot { width: 12px; height: 12px; border-radius: 50%; margin-top: 5px; background: var(--line); }
ol.road .done .dot { background: var(--good); }
ol.road .active .dot { background: var(--accent); }
ol.road .blocked .dot { background: var(--warn); }
pre { white-space: pre-wrap; background: var(--card); border: 1px solid var(--line); border-radius: 10px;
      padding: 12px; font-size: 12px; }
svg { display: block; width: 100%; }
footer { max-width: 1200px; margin: 0 auto; padding: 8px 16px 32px; color: var(--muted); font-size: 12px; }
#err { color: var(--bad); }
</style>
</head>
<body>
<header>
  <h1>OFI scalper: status</h1>
  <p>Binance USD&#9416;-M BTCUSDT / ETHUSDT &middot; read-only view for colleagues
     &middot; <span id="mode"></span></p>
</header>
<nav><div id="tabs">
  <a href="#overview" class="on">Overview</a><a href="#data">Data collection</a>
  <a href="#health">Live health</a><a href="#research">Research</a><a href="#trading">Trading</a>
</div></nav>
<main>
  <div id="err"></div>
  <section id="overview" class="on">
    <div class="grid" id="ov-cards"></div>
    <h2>Milestones</h2><div class="grid" id="ov-milestones"></div>
    <h2>Roadmap</h2><div class="card"><ol class="road" id="ov-road"></ol></div>
  </section>
  <section id="data">
    <div class="grid" id="dc-cards"></div>
    <h2>Recorded per day (MB)</h2><div class="card" id="dc-chart"></div>
    <h2>Days</h2><div class="card scroll" id="dc-days"></div>
  </section>
  <section id="health">
    <div class="grid" id="hl-cards"></div>
    <h2>Last 24 hours</h2><div class="grid" id="hl-spark"></div>
    <h2>Order books</h2><div class="card scroll" id="hl-books"></div>
  </section>
  <section id="research">
    <div id="rs-body"></div>
  </section>
  <section id="trading">
    <div id="tr-body"></div>
  </section>
</main>
<footer>Updated <span id="gen">-</span>. Refreshes every 30 seconds. Nothing on this page can change
the system; it only reads.</footer>
<script>
"use strict";
const $ = (id) => document.getElementById(id);
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
const num = (v, d = 2) => (v === null || v === undefined || v === "") ? "-" : Number(v).toFixed(d);
const pct = (v) => (v === null || v === undefined) ? "-" : (Number(v) * 100).toFixed(1) + "%";
const cls = (v) => v > 0 ? "good" : v < 0 ? "bad" : "";
function card(title, value, sub = "", tone = "") {
  return `<div class="card"><h3>${esc(title)}</h3><div class="big ${tone}">${value}</div>` +
         (sub ? `<div class="muted">${sub}</div>` : "") + `</div>`;
}
function table(rows, cols) {
  if (!rows || !rows.length) return `<div class="muted">Nothing yet.</div>`;
  return "<table><tr>" + cols.map((c) => `<th>${esc(c[0])}</th>`).join("") + "</tr>" +
    rows.map((r) => "<tr>" + cols.map((c) => `<td>${c[1](r)}</td>`).join("") + "</tr>").join("") +
    "</table>";
}
function bars(values, labels) {
  if (!values.length) return `<div class="muted">No complete days yet.</div>`;
  const w = 600, h = 140, max = Math.max(...values, 1), bw = w / values.length;
  const rects = values.map((v, i) => {
    const bh = Math.max(1, (v / max) * (h - 20));
    return `<rect x="${i * bw + 1}" y="${h - bh}" width="${Math.max(bw - 2, 1)}" height="${bh}"` +
           ` fill="var(--bar)"><title>${esc(labels[i])}: ${num(v, 0)} MB</title></rect>`;
  }).join("");
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img"` +
         ` aria-label="MB recorded per day">${rects}</svg>` +
         `<div class="muted">${esc(labels[0])} &rarr; ${esc(labels[labels.length - 1])}, max ${num(max, 0)} MB</div>`;
}
function spark(points, key, label, unit) {
  const vals = points.map((p) => p[key]).filter((v) => v !== null && v !== undefined);
  if (vals.length < 2) return card(label, "-", "collecting (one point a minute)");
  const w = 300, h = 60, lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
  const step = w / (vals.length - 1);
  const path = vals.map((v, i) => `${i ? "L" : "M"}${(i * step).toFixed(1)},${(h - 4 - ((v - lo) / span) * (h - 8)).toFixed(1)}`).join("");
  const svg = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="${esc(label)}">` +
              `<path d="${path}" fill="none" stroke="var(--accent)" stroke-width="1.5"/></svg>`;
  return `<div class="card"><h3>${esc(label)}</h3><div class="big">${num(vals[vals.length - 1], 1)}` +
         ` <span class="muted" style="font-size:13px">${esc(unit)}</span></div>${svg}` +
         `<div class="muted">min ${num(lo, 1)} &middot; max ${num(hi, 1)}</div></div>`;
}
function svcChips(services) {
  return Object.entries(services || {}).map(([name, st]) =>
    `<span class="chip ${st === "active" ? "good" : (st === "inactive" || st === "failed") ? "bad" : ""}">` +
    `${esc(name)}: ${esc(st)}</span>`).join("");
}
function overview(s) {
  const c = s.collection, h = s.health;
  const scalper = h.scalper_reachable ? (h.ready ? `<span class="good">recording</span>` : `<span class="warn">starting</span>`)
                                      : `<span class="bad">down</span>`;
  const runway = c.disk_runway_days;
  $("ov-cards").innerHTML =
    card("Scalper", scalper, h.scalper_reachable ? `mode ${esc(h.execution_mode)}${h.model_version ? ", model " + esc(h.model_version) : ", no model"}`
                                                 : `last seen ${esc(h.last_ok_at || "never")}`) +
    card("Clean days recorded", `${c.eligible_days}`, `of ${c.recorded_days} complete days; ${num(c.total_gb, 1)} GB in total`) +
    card("Disk", `${num(c.disk_free_gb, 1)} GB free`,
         runway === null ? "runway unknown yet" : `about ${num(runway, 0)} days of recording left`,
         runway !== null && runway < 7 ? "bad" : "") +
    `<div class="card"><h3>Services</h3>${svcChips(h.services)}</div>`;
  $("ov-milestones").innerHTML = Object.values(c.milestones).map((m) =>
    `<div class="card"><h3>${esc(m.label)}</h3><div class="big">${m.eligible_days} / ${m.target_days} days</div>` +
    `<div class="bar"><div style="width:${(m.progress * 100).toFixed(1)}%"></div></div>` +
    `<div class="muted">${m.periods} / ${m.target_periods} ${esc(m.period)}s with data &middot; ` +
    `${m.earliest === "reached" ? "data reached" : "earliest " + esc(m.earliest)}</div></div>`).join("");
  $("ov-road").innerHTML = (s.roadmap || []).map((r) =>
    `<li class="${esc(r.status)}"><span class="dot"></span><div><b>${esc(r.phase)}</b>` +
    ` <span class="muted">${esc(r.status)}${r.date ? " &middot; " + esc(r.date) : ""}</span>` +
    `<div class="muted">${esc(r.note)}</div></div></li>`).join("");
}
function collection(s) {
  const c = s.collection;
  $("dc-cards").innerHTML =
    card("Complete days", c.recorded_days, `${c.eligible_days} clean enough for research`) +
    card("Excluded", c.excluded.length, c.excluded.length ? "see the table below" : "none") +
    card("Size", `${num(c.total_gb, 1)} GB`, c.gb_per_day ? `${num(c.gb_per_day, 2)} GB per day (last 7)` : "") +
    card("Disk floor", `${num(c.disk_floor_gb, 0)} GB`, "the recorder pauses below this");
  const complete = c.days.filter((d) => d.complete).slice(0, 60).reverse();
  $("dc-chart").innerHTML = bars(complete.map((d) => d.mb), complete.map((d) => d.date));
  const sym = s.symbols || [];
  $("dc-days").innerHTML = table(c.days, [
    ["Date", (d) => esc(d.date)],
    ["Status", (d) => d.complete ? (d.eligible ? `<span class="good">clean</span>` : `<span class="bad">excluded</span>`)
                                 : `<span class="warn">in progress</span>`],
    ...sym.map((x) => [x + " hours", (d) => d.symbols[x] ? d.symbols[x].hours : "-"]),
    ["MB", (d) => num(d.mb, 0)],
    ...sym.map((x) => [x + " breaks", (d) => d.symbols[x] && d.symbols[x].depth_breaks !== undefined
        ? `${d.symbols[x].depth_breaks}${d.symbols[x].unrecovered_breaks ? " (" + d.symbols[x].unrecovered_breaks + " unrecovered)" : ""}` : "-"]),
    ["Note", (d) => `<span class="muted">${esc(d.reason)}</span>`],
  ]);
}
function health(s, points) {
  const h = s.health;
  if (!h.scalper_reachable) {
    $("hl-cards").innerHTML = card("Scalper", `<span class="bad">not reachable</span>`, `last seen ${esc(h.last_ok_at || "never")}`) +
      `<div class="card"><h3>Services</h3>${svcChips(h.services)}</div>`;
    $("hl-books").innerHTML = "";
  } else {
    const lag = h.lag_ms || {}, rec = h.recorder || {};
    const pauses = Object.entries(h.pauses || {}).filter(([, r]) => r.length);
    $("hl-cards").innerHTML =
      card("State", h.ready ? `<span class="good">ready</span>` : `<span class="warn">not ready</span>`,
           `up ${num((h.uptime_s || 0) / 3600, 1)} h &middot; ${esc(h.book_mode)} book`) +
      card("Depth feed lag", lag.depth ? `${num(lag.depth.p50, 1)} ms` : "-", lag.depth ? `p99 ${num(lag.depth.p99, 1)} ms` : "") +
      card("Trade feed lag", lag.aggTrade ? `${num(lag.aggTrade.p50, 1)} ms` : "-", lag.aggTrade ? `p99 ${num(lag.aggTrade.p99, 1)} ms` : "") +
      card("Recorder", `${num(rec.lines_per_s, 0)} lines/s`, rec.disk_paused ? `<span class="bad">paused: disk full</span>` : `dropped ${rec.dropped ?? 0}, errors ${rec.errors ?? 0}`) +
      card("Risk", h.halted ? `<span class="bad">halted</span>` : `<span class="good">normal</span>`,
           h.halted ? esc(h.halt_reason) : pauses.length ? "paused: " + esc(pauses.map(([k, r]) => k + " " + r.join(",")).join("; ")) : "no pauses");
    $("hl-books").innerHTML = table(Object.entries(h.books || {}).map(([k, b]) => ({ k, ...b })), [
      ["Symbol", (b) => esc(b.k)], ["State", (b) => esc(b.state)], ["Gaps", (b) => esc(b.gaps)],
      ["Resyncs", (b) => esc(b.resyncs)],
    ]) + `<div class="muted" style="margin-top:6px">Reconnects: ${esc(JSON.stringify(h.reconnects || {}))}</div>`;
  }
  $("hl-spark").innerHTML = spark(points, "depth_lag_p50", "Depth lag p50", "ms") +
    spark(points, "depth_lag_p99", "Depth lag p99", "ms") + spark(points, "depth_per_s", "Book updates", "/s") +
    spark(points, "lines_per_s", "Recorder", "lines/s") + spark(points, "disk_free_gb", "Disk free", "GB");
}
function research(s) {
  const r = s.research;
  let out = "";
  out += `<h2>Runs</h2><div class="card scroll">` + table(r.runs, [
    ["Run", (x) => esc(x.run)], ["Kind", (x) => x.binding ? "binding" : "provisional"],
    ["Walk-forward", (x) => esc(x.walk_forward.join(", "))], ["Held out", (x) => esc(x.held_out.join(", "))],
    ["Days", (x) => esc(x.days)], ["Created", (x) => esc((x.created_at || "").slice(0, 10))],
  ]) + `</div>`;
  out += `<h2>Candidate models (one per horizon)</h2><div class="card scroll">` + table(r.candidates, [
    ["Run", (x) => esc(x.run)], ["Horizon", (x) => esc(x.horizon_s) + " s"], ["Barrier", (x) => esc(x.barrier_bp) + " bp"],
    ["Folds", (x) => esc(x.folds)], ["Training rows", (x) => esc(x.train_rows)],
    ["Brier up / none / down", (x) => `${num(x.brier.up, 4)} / ${num(x.brier.none, 4)} / ${num(x.brier.down, 4)}`],
  ]) + `</div><div class="muted">Brier score of the calibrated probabilities on out-of-fold data: lower is better.</div>`;
  for (const sel of r.selections) {
    out += `<h2>Selection: ${esc(sel.run)}</h2><div class="card scroll">` +
      `<div class="muted">${esc(sel.rule)}</div>` +
      (sel.chosen ? `<p>Chosen: <b>${esc(sel.chosen.name)}</b>, ${esc(sel.chosen.trades)} trades, net $${num(sel.chosen.net_usd, 2)}</p>`
                  : `<p class="warn">No horizon and threshold reached the minimum trade count.</p>`) +
      table(sel.grid, [["Arm", (x) => esc(x.name)], ["Trades", (x) => esc(x.trades)],
        ["Net $", (x) => `<span class="${cls(x.net_usd)}">${num(x.net_usd, 2)}</span>`],
        ["Mean net bp", (x) => num(x.mean_net_bp, 2)], ["Win rate", (x) => pct(x.win_rate)]]) + `</div>`;
  }
  out += `<h2>Gate results</h2>`;
  if (!r.models.length) out += `<div class="card muted">No model has been through the gates yet.</div>`;
  for (const m of r.models) {
    out += `<div class="card scroll" style="margin-bottom:12px"><h3>${esc(m.version)}</h3>` +
      `<div class="big ${m.passed ? "good" : "bad"}">${m.passed ? "passed" : "did not pass"}</div>` +
      `<div class="muted">${m.binding ? "binding run" : "provisional run"} &middot; ${m.held_out_days} held-out days &middot; ${esc(m.evaluated_at)}</div>` +
      table(Object.entries(m.gates).map(([k, g]) => ({ k, ...g })), [
        ["Gate", (g) => esc(g.k)], ["Value", (g) => esc(typeof g.value === "number" ? num(g.value, 3) : g.value)],
        ["Threshold", (g) => esc(g.threshold)], ["", (g) => g.passed ? `<span class="good">pass</span>` : `<span class="bad">fail</span>`],
      ]) + `</div>`;
  }
  if (r.report) out += `<h2>Latest report</h2><pre>${esc(r.report)}</pre>`;
  if (!r.runs.length && !r.models.length) {
    out = `<div class="card"><h3>Research</h3><div>No research run yet. The first provisional run needs about four ` +
          `weeks of clean recordings; see the milestones on the Overview tab.</div></div>` + out;
  }
  $("rs-body").innerHTML = out;
}
function trading(s) {
  const t = s.trading;
  const sum = (x, label) => card(label, `<span class="${cls(x.net_usd)}">$${num(x.net_usd, 2)}</span>`,
    `${x.trades} trades &middot; win ${pct(x.win_rate)} &middot; fill ${pct(x.maker_fill_rate)} &middot; fees $${num(x.fees_usd, 2)}`);
  let out = t.note ? `<div class="card" style="margin-bottom:12px">${esc(t.note)}</div>` : "";
  out += `<div class="grid">${sum(t.today, "Today")}${sum(t.last_7_days, "Last 7 days")}` +
         card("Signals (7 days)", t.last_7_days.signals, `Brier on own fills ${num(t.last_7_days.brier, 3)}`) + `</div>`;
  out += `<h2>Recent cycles</h2><div class="card scroll">` + table(t.recent_trades, [
    ["Signal", (x) => esc((x.signal_at || "").replace("T", " ").slice(0, 19))], ["Symbol", (x) => esc(x.symbol)],
    ["Side", (x) => esc(x.side)], ["p", (x) => num(x.p, 3)], ["Filled", (x) => x.filled ? "yes" : "no"],
    ["Entry", (x) => num(x.entry_price, 2)], ["Exit", (x) => num(x.exit_price, 2)], ["Reason", (x) => esc(x.exit_reason)],
    ["Net bp", (x) => `<span class="${cls(x.net_bp)}">${num(x.net_bp, 2)}</span>`], ["Mode", (x) => esc(x.mode)],
  ]) + `</div>`;
  out += `<h2>Recent signals</h2><div class="card scroll">` + table(t.recent_signals, [
    ["Time", (x) => esc((x.at || "").replace("T", " ").slice(0, 19))], ["Symbol", (x) => esc(x.symbol)],
    ["Side", (x) => esc(x.side)], ["p", (x) => num(x.p, 3)], ["Threshold", (x) => num(x.threshold, 3)],
    ["Action", (x) => esc(x.action)],
  ]) + `</div>`;
  $("tr-body").innerHTML = out;
}
let points = [];
async function load() {
  try {
    const r = await fetch("api/summary", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const s = await r.json();
    $("err").textContent = "";
    $("gen").textContent = s.generated_at.replace("T", " ").replace("+00:00", " UTC");
    $("mode").textContent = s.health.scalper_reachable ? "scalper " + (s.health.ready ? "running" : "starting")
                                                       : "scalper unreachable";
    overview(s); collection(s); health(s, points); research(s); trading(s);
  } catch (e) {
    $("err").textContent = "Could not refresh: " + e.message;
  }
}
async function loadHistory() {
  try {
    const r = await fetch("api/history", { cache: "no-store" });
    if (r.ok) points = (await r.json()).points || [];
  } catch (e) { /* keep the last series */ }
}
function show(hash) {
  const id = (hash || "#overview").slice(1);
  document.querySelectorAll("section").forEach((s) => s.classList.toggle("on", s.id === id));
  document.querySelectorAll("#tabs a").forEach((a) => a.classList.toggle("on", a.getAttribute("href") === "#" + id));
}
window.addEventListener("hashchange", () => show(location.hash));
show(location.hash);
loadHistory().then(load);
setInterval(load, 30000);
setInterval(loadHistory, 60000);
</script>
</body>
</html>
"""
