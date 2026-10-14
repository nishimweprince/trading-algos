#include "bpmm/dashboard.hpp"

#include "bpmm/types.hpp"

#include <algorithm>
#include <cstdlib>
#include <fstream>
#include <sstream>

namespace bpmm {
namespace {

template <typename T>
void trim_front(std::vector<T>& values, std::size_t max) {
  if (values.size() > max) {
    const auto drop =
        static_cast<typename std::vector<T>::difference_type>(values.size() - max);
    values.erase(values.begin(), values.begin() + drop);
  }
}

void parse_fill_line(std::string_view line, PnlTracker& tracker, std::vector<DashboardFill>& out) {
  // F fill_ns order_id side price_raw quantity_raw fee_raw
  std::istringstream row{std::string(line)};
  char kind = 0;
  DashboardFill fill;
  row >> kind >> fill.fill_ns >> fill.order_id >> fill.side >> fill.price_raw >>
      fill.quantity_raw >> fill.fee_raw;
  if (!row || (fill.side != 0 && fill.side != 1) || fill.quantity_raw <= 0) return;
  tracker.apply(fill.side, fill.price_raw, fill.quantity_raw, fill.fee_raw);
  fill.realized_pnl_raw = tracker.realized_pnl_raw();
  fill.position_raw = tracker.position_raw();
  out.push_back(fill);
}

void parse_candle_line(std::string_view line, std::vector<DashboardCandle>& out) {
  // K open_ms close_ms open high low close closed
  std::istringstream row{std::string(line)};
  char kind = 0;
  DashboardCandle candle;
  row >> kind >> candle.open_ms >> candle.close_ms >> candle.open_raw >> candle.high_raw >>
      candle.low_raw >> candle.close_raw >> candle.closed;
  if (!row) return;
  out.push_back(candle);
}

}  // namespace

void PnlTracker::apply(int side, std::int64_t price_raw, std::int64_t quantity_raw,
                       std::int64_t fee_raw) noexcept {
  const std::int64_t signed_qty = side == 0 ? quantity_raw : -quantity_raw;
  const std::int64_t old_qty = quantity_;
  const std::int64_t new_qty = old_qty + signed_qty;
  if (old_qty == 0 || (old_qty > 0) == (signed_qty > 0)) {
    const std::int64_t old_notional = mul_scaled(average_entry_raw_, std::llabs(old_qty));
    const std::int64_t add_notional = mul_scaled(price_raw, std::llabs(signed_qty));
    average_entry_raw_ = new_qty == 0
                             ? 0
                             : ((old_notional + add_notional) * kScale) /
                                   std::max<std::int64_t>(1, std::llabs(new_qty));
  } else {
    const std::int64_t closed = std::min(std::llabs(old_qty), std::llabs(signed_qty));
    const std::int64_t per_unit =
        old_qty > 0 ? price_raw - average_entry_raw_ : average_entry_raw_ - price_raw;
    realized_ += mul_scaled(per_unit, closed) - fee_raw;
    if (new_qty == 0) {
      average_entry_raw_ = 0;
    } else if ((new_qty > 0) != (old_qty > 0)) {
      average_entry_raw_ = price_raw;
    }
  }
  quantity_ = new_qty;
}

DashboardActivity read_journal_tail(const std::string& path, DashboardLimits limits) {
  DashboardActivity activity;
  std::ifstream input(path, std::ios::binary | std::ios::ate);
  if (!input) return activity;
  const auto size = input.tellg();
  if (size <= 0) return activity;
  const auto size_bytes = static_cast<std::uint64_t>(size);
  const auto span_bytes = size_bytes > limits.max_bytes
                              ? static_cast<std::uint64_t>(limits.max_bytes)
                              : size_bytes;
  input.seekg(static_cast<std::streamoff>(size_bytes - span_bytes));
  std::string data(static_cast<std::size_t>(span_bytes), '\0');
  input.read(data.data(), static_cast<std::streamsize>(span_bytes));
  data.resize(static_cast<std::size_t>(input.gcount()));
  if (size_bytes > limits.max_bytes) {
    const auto cut = data.find('\n');
    if (cut == std::string::npos) return activity;
    data.erase(0, cut + 1);
  }
  PnlTracker tracker;
  std::size_t begin = 0;
  while (begin < data.size()) {
    const auto end = data.find('\n', begin);
    if (end == std::string::npos) break;  // trailing partial line: recorder mid-write
    const std::string_view line(data.data() + begin, end - begin);
    begin = end + 1;
    if (line.starts_with("F ")) {
      parse_fill_line(line, tracker, activity.fills);
    } else if (line.starts_with("K ")) {
      parse_candle_line(line, activity.candles);
    }
  }
  trim_front(activity.fills, limits.max_fills);
  trim_front(activity.candles, limits.max_candles);
  return activity;
}

std::string activity_json(const DashboardActivity& activity) {
  std::string out = "{\"scale\":";
  out += std::to_string(kScale);
  out += ",\"fills\":[";
  bool first = true;
  for (const auto& fill : activity.fills) {
    if (!first) out += ',';
    first = false;
    out += "{\"t\":" + std::to_string(fill.fill_ns) + ",\"id\":" + std::to_string(fill.order_id) +
           ",\"s\":" + std::to_string(fill.side) + ",\"p\":" + std::to_string(fill.price_raw) +
           ",\"q\":" + std::to_string(fill.quantity_raw) + ",\"f\":" + std::to_string(fill.fee_raw) +
           ",\"r\":" + std::to_string(fill.realized_pnl_raw) + ",\"n\":" +
           std::to_string(fill.position_raw) + '}';
  }
  out += "],\"candles\":[";
  first = true;
  for (const auto& candle : activity.candles) {
    if (!first) out += ',';
    first = false;
    out += "{\"om\":" + std::to_string(candle.open_ms) + ",\"cm\":" +
           std::to_string(candle.close_ms) + ",\"o\":" + std::to_string(candle.open_raw) +
           ",\"h\":" + std::to_string(candle.high_raw) + ",\"l\":" + std::to_string(candle.low_raw) +
           ",\"c\":" + std::to_string(candle.close_raw) + ",\"x\":" +
           (candle.closed ? "1" : "0") + '}';
  }
  out += "]}";
  return out;
}

const char kPage[] = R"page(<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>bar-mm shadow</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,400..700&display=swap" rel="stylesheet">
<style>
  :root {
    --paper: #e9efe8;
    --panel: #f5f8f4;
    --ink: #17211b;
    --muted: #4c5a51;
    --line: #c4d0c5;
    --gain: #166b3a;
    --loss: #96292a;
    --teal: #0e5a64;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--paper);
    color: var(--ink);
    font-family: "DM Sans", ui-sans-serif, system-ui, sans-serif;
    font-size: 12.5px;
    line-height: 1.4;
    font-variant-numeric: tabular-nums;
  }
  main { max-width: 62rem; margin: 0 auto; padding: 1.25rem 1rem 3rem; }
  header { display: flex; align-items: baseline; gap: 0.75rem; flex-wrap: wrap; }
  header h1 { font-size: 1rem; font-weight: 700; margin: 0; }
  header .symbol { color: var(--muted); font-weight: 600; }
  header .spacer { flex: 1; }
  header .updated { color: var(--muted); font-size: 0.85rem; }
  .pill {
    display: inline-block;
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    padding: 0.1rem 0.55rem;
    border-radius: 999px;
    border: 1px solid var(--line);
    color: var(--muted);
    background: var(--panel);
  }
  .pill.ready { color: var(--gain); border-color: var(--gain); }
  .pill.halted, .pill.offline { color: var(--loss); border-color: var(--loss); }
  .hero {
    display: flex; gap: 2rem; align-items: baseline; flex-wrap: wrap;
    margin: 1rem 0 0; padding: 0.85rem 1rem;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 8px;
  }
  .hero .pnl { font-size: 1.75rem; font-weight: 700; }
  .hero .pnl.up { color: var(--gain); }
  .hero .pnl.down { color: var(--loss); }
  .hero dl { display: flex; gap: 1.75rem; margin: 0; flex-wrap: wrap; }
  .hero dt { font-size: 0.75rem; color: var(--muted); font-weight: 600; }
  .hero dd { margin: 0.1rem 0 0; font-size: 1rem; font-weight: 600; }
  section { margin-top: 1.5rem; }
  section > h2 { font-size: 0.85rem; font-weight: 700; margin: 0 0 0.5rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(8.5rem, 1fr)); gap: 0.5rem; }
  .stat { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 0.5rem 0.65rem; }
  .stat .k { font-size: 0.72rem; color: var(--muted); font-weight: 600; }
  .stat .v { font-size: 1.02rem; font-weight: 600; margin-top: 0.1rem; }
  .stat .v.up { color: var(--gain); }
  .stat .v.down { color: var(--loss); }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 0.6rem 0.65rem 0.35rem; }
  .panel h3 { font-size: 0.8rem; font-weight: 700; margin: 0 0 0.25rem; }
  .panel h3 .hint { font-weight: 400; color: var(--muted); }
  svg.chart { display: block; width: 100%; }
  svg text { font-size: 9px; fill: var(--muted); }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: right; padding: 0.28rem 0.5rem; border-bottom: 1px solid var(--line); }
  th:first-child, td:first-child { text-align: left; }
  thead th { font-size: 0.72rem; color: var(--muted); font-weight: 600; }
  tbody td { font-size: 0.85rem; }
  td.side-buy { color: var(--gain); font-weight: 700; }
  td.side-sell { color: var(--loss); font-weight: 700; }
  .empty { color: var(--muted); padding: 0.5rem 0; }
  footer { margin-top: 1.5rem; color: var(--muted); font-size: 0.78rem; }
</style>
</head>
<body>
<main>
  <header>
    <h1>bar-mm shadow</h1>
    <span class="symbol" id="symbol">&ndash;</span>
    <span class="pill" id="pill">CONNECTING</span>
    <span class="spacer"></span>
    <span class="updated" id="updated"></span>
  </header>

  <div class="hero">
    <span class="pnl" id="heroPnl">&ndash;</span>
    <dl>
      <div><dt>Unrealized</dt><dd id="heroUnreal">&ndash;</dd></div>
      <div><dt>Equity (realized)</dt><dd id="heroEquity">&ndash;</dd></div>
      <div><dt>Position</dt><dd id="heroPos">&ndash;</dd></div>
    </dl>
  </div>

  <section>
    <h2>Session</h2>
    <div class="grid">
      <div class="stat"><div class="k">Entry</div><div class="v" id="stEntry">&ndash;</div></div>
      <div class="stat"><div class="k">Mid</div><div class="v" id="stMid">&ndash;</div></div>
      <div class="stat"><div class="k">Fills</div><div class="v" id="stFills">&ndash;</div></div>
      <div class="stat"><div class="k">Intents</div><div class="v" id="stIntents">&ndash;</div></div>
      <div class="stat"><div class="k">Risk rejects</div><div class="v" id="stRejects">&ndash;</div></div>
      <div class="stat"><div class="k">Gaps</div><div class="v" id="stGaps">&ndash;</div></div>
      <div class="stat"><div class="k">Depth events</div><div class="v" id="stDepth">&ndash;</div></div>
      <div class="stat"><div class="k">Trade events</div><div class="v" id="stTrades">&ndash;</div></div>
      <div class="stat"><div class="k">Closed candles</div><div class="v" id="stCandles">&ndash;</div></div>
    </div>
  </section>

  <section>
    <h2>Performance</h2>
    <div class="panel">
      <h3>Realized PnL <span class="hint">USDT, cumulative from journal fills</span></h3>
      <svg class="chart" id="svgPnl" height="110"></svg>
    </div>
  </section>

  <section>
    <div class="panel">
      <h3>Position <span class="hint">net base units from journal fills</span></h3>
      <svg class="chart" id="svgPos" height="90"></svg>
    </div>
  </section>

  <section>
    <div class="panel">
      <h3>Close &amp; Bar Portion <span class="hint">1-minute candles; bars are BP in [-1, 1]</span></h3>
      <svg class="chart" id="svgPx" height="130"></svg>
    </div>
  </section>

  <section>
    <h2>Recent fills</h2>
    <div class="panel">
      <table>
        <thead><tr><th>Time</th><th>Side</th><th>Price</th><th>Qty</th><th>Fee</th><th>Cum PnL</th></tr></thead>
        <tbody id="fillsBody"><tr><td colspan="6" class="empty">No fills yet.</td></tr></tbody>
      </table>
    </div>
  </section>

  <footer>
    Snapshot refreshes every 5 seconds. Curves, fills, and candles come from the current journal
    file only; rotated journals are not included. Headline counters cover the process lifetime.
    Realized PnL deducts fees on the closing leg, matching engine accounting. Unrealized is
    (mid &minus; entry) &times; position and is indicative. Shadow only: no orders leave this host.
  </footer>
</main>
<script>
"use strict";
const POLL_MS = 5000;
const EQUITY_BASE_USDT = 10000;
let scale = 100000000;
let lastOk = 0;

const $ = (id) => document.getElementById(id);
const usdt = (raw) => raw / scale;
const money = (raw) => (usdt(raw) < 0 ? "-" : "") + "$" + Math.abs(usdt(raw)).toFixed(2);
const num = (raw, digits) => {
  const v = raw / scale;
  return parseFloat(v.toFixed(digits === undefined ? 8 : digits)).toString();
};
const clock = (ns) => {
  const d = new Date(Math.floor(Number(ns) / 1e6));
  return d.toTimeString().slice(0, 8);
};
const setDir = (el, raw) => {
  el.classList.toggle("up", raw > 0);
  el.classList.toggle("down", raw < 0);
};

function line(svg, values, fmt) {
  const W = Math.max(svg.clientWidth || 600, 200);
  const H = Number(svg.getAttribute("height")) || 110;
  const padL = 52, padR = 56, padT = 8, padB = 8;
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  const innerW = W - padL - padR, innerH = H - padT - padB;
  let lo = Infinity, hi = -Infinity;
  for (const v of values) { if (v < lo) lo = v; if (v > hi) hi = v; }
  if (!isFinite(lo)) { lo = -1; hi = 1; }
  if (lo === hi) { lo -= 1; hi += 1; }
  const X = (i) => padL + (values.length < 2 ? innerW / 2 : (i * innerW) / (values.length - 1));
  const Y = (v) => padT + innerH - ((v - lo) / (hi - lo)) * innerH;
  const ink = "#17211b", grid = "#c4d0c5";
  let s = "";
  if (lo < 0 && hi > 0) {
    const zy = Y(0);
    s += '<line x1="' + padL + '" y1="' + zy + '" x2="' + (W - padR) + '" y2="' + zy +
         '" stroke="' + grid + '" stroke-dasharray="3 3"/>';
  }
  if (values.length > 0) {
    let d = "M" + X(0).toFixed(1) + " " + Y(values[0]).toFixed(1);
    for (let i = 1; i < values.length; ++i) d += "L" + X(i).toFixed(1) + " " + Y(values[i]).toFixed(1);
    s += '<path d="' + d + '" fill="none" stroke="' + ink + '" stroke-width="1.25"/>';
    const lx = X(values.length - 1), ly = Y(values[values.length - 1]);
    s += '<circle cx="' + lx.toFixed(1) + '" cy="' + ly.toFixed(1) + '" r="2.5" fill="' + ink + '"/>';
    s += '<text x="' + (W - padR + 6) + '" y="' + (ly + 3).toFixed(1) + '">' + fmt(values[values.length - 1]) + "</text>";
  }
  s += '<text x="4" y="' + (padT + 8) + '">' + fmt(hi) + "</text>";
  s += '<text x="4" y="' + (H - 2) + '">' + fmt(lo) + "</text>";
  svg.innerHTML = s;
}

function priceBp(svg, candles) {
  const W = Math.max(svg.clientWidth || 600, 200);
  const H = Number(svg.getAttribute("height")) || 130;
  const padL = 52, padR = 56, padT = 8, padB = 8;
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  const innerW = W - padL - padR, innerH = H - padT - padB;
  const ink = "#17211b", teal = "#0e5a64", grid = "#c4d0c5";
  const closes = candles.map((c) => usdt(c.c));
  let lo = Infinity, hi = -Infinity;
  for (const v of closes) { if (v < lo) lo = v; if (v > hi) hi = v; }
  if (!isFinite(lo)) lo = 0, hi = 1;
  if (lo === hi) { lo -= 1; hi += 1; }
  const n = candles.length;
  const X = (i) => padL + (n < 2 ? innerW / 2 : (i * innerW) / (n - 1));
  const Y = (v) => padT + innerH - ((v - lo) / (hi - lo)) * innerH;
  const bw = n > 0 ? Math.max(1, Math.min(7, innerW / n - 1)) : 1;
  const bpZeroY = padT + innerH / 2;
  let s = '<line x1="' + padL + '" y1="' + bpZeroY + '" x2="' + (W - padR) + '" y2="' + bpZeroY +
          '" stroke="' + grid + '" stroke-dasharray="3 3"/>';
  for (let i = 0; i < n; ++i) {
    const c = candles[i];
    const h = usdt(c.h), l = usdt(c.l);
    const bp = h === l ? 0 : (usdt(c.c) - usdt(c.o)) / (h - l);
    const bh = Math.abs(bp) * innerH * 0.5;
    const by = bp >= 0 ? bpZeroY - bh : bpZeroY;
    s += '<rect x="' + (X(i) - bw / 2).toFixed(1) + '" y="' + by.toFixed(1) +
         '" width="' + bw.toFixed(1) + '" height="' + Math.max(bh, bp === 0 ? 1 : 0.5).toFixed(1) +
         '" fill="' + teal + '" opacity="0.55"/>';
  }
  if (closes.length > 0) {
    let d = "M" + X(0).toFixed(1) + " " + Y(closes[0]).toFixed(1);
    for (let i = 1; i < closes.length; ++i) d += "L" + X(i).toFixed(1) + " " + Y(closes[i]).toFixed(1);
    s += '<path d="' + d + '" fill="none" stroke="' + ink + '" stroke-width="1.25"/>';
    s += '<text x="' + (W - padR + 6) + '" y="' + (Y(closes[closes.length - 1]) + 3).toFixed(1) + '">' +
         closes[closes.length - 1].toFixed(2) + "</text>";
  }
  s += '<text x="4" y="' + (padT + 8) + '">' + hi.toFixed(2) + "</text>";
  s += '<text x="4" y="' + (H - 2) + '">' + lo.toFixed(2) + "</text>";
  svg.innerHTML = s;
}

function renderSummary(j) {
  $("symbol").textContent = j.symbol || "?";
  const pill = $("pill");
  pill.classList.remove("ready", "halted", "offline");
  if (j.halted) { pill.textContent = "HALTED"; pill.classList.add("halted"); }
  else if (j.ready) { pill.textContent = "READY"; pill.classList.add("ready"); }
  else { pill.textContent = "STARTING"; }
  const pnl = $("heroPnl");
  pnl.textContent = money(j.realized_pnl_raw);
  setDir(pnl, j.realized_pnl_raw);
  const unrealRaw = Math.round((usdt(j.mid_raw) - usdt(j.entry_raw)) * j.position_raw);
  const unreal = $("heroUnreal");
  unreal.textContent = j.position_raw === 0 ? "$0.00" : money(unrealRaw);
  setDir(unreal, j.position_raw === 0 ? 0 : unrealRaw);
  $("heroEquity").textContent = money(EQUITY_BASE_USDT * scale + j.realized_pnl_raw);
  const pos = $("heroPos");
  pos.textContent = (j.position_raw > 0 ? "+" : "") + num(j.position_raw, 4);
  setDir(pos, j.position_raw);
  $("stEntry").textContent = j.position_raw === 0 ? "--" : num(j.entry_raw);
  $("stMid").textContent = j.mid_raw === 0 ? "--" : num(j.mid_raw);
  $("stFills").textContent = j.fills;
  $("stIntents").textContent = j.intents;
  $("stRejects").textContent = j.risk_rejects;
  $("stGaps").textContent = j.gaps;
  $("stDepth").textContent = j.depth_events;
  $("stTrades").textContent = j.trade_events;
  $("stCandles").textContent = j.closed_candles;
}

function renderActivity(j) {
  if (j.scale) scale = j.scale;
  const fills = j.fills || [];
  const candles = (j.candles || []).filter((c) => c.x === 1);
  line($("svgPnl"), fills.map((f) => usdt(f.r)), (v) => "$" + v.toFixed(2));
  line($("svgPos"), fills.map((f) => usdt(f.n)), (v) => "" + parseFloat(v.toFixed(4)));
  priceBp($("svgPx"), candles);
  const body = $("fillsBody");
  if (fills.length === 0) {
    body.innerHTML = '<tr><td colspan="6" class="empty">No fills yet.</td></tr>';
    return;
  }
  const rows = fills.slice(-15).reverse().map((f) => {
    const buy = f.s === 0;
    return "<tr><td>" + clock(f.t) + "</td>" +
      '<td class="' + (buy ? "side-buy" : "side-sell") + '">' + (buy ? "BUY" : "SELL") + "</td>" +
      "<td>" + num(f.p) + "</td><td>" + num(f.q, 4) + "</td><td>" + num(f.f) + "</td><td>" +
      money(f.r) + "</td></tr>";
  });
  body.innerHTML = rows.join("");
}

async function tick() {
  try {
    const [s, a] = await Promise.all([
      fetch("/api/summary", { cache: "no-store" }).then((r) => { if (!r.ok) throw 0; return r.json(); }),
      fetch("/api/activity", { cache: "no-store" }).then((r) => { if (!r.ok) throw 0; return r.json(); }),
    ]);
    lastOk = Date.now();
    renderSummary(s);
    renderActivity(a);
    $("updated").textContent = "updated " + new Date(lastOk).toTimeString().slice(0, 8);
  } catch (e) {
    const pill = $("pill");
    pill.textContent = "OFFLINE";
    pill.classList.remove("ready", "halted");
    pill.classList.add("offline");
    $("updated").textContent = lastOk === 0 ? "no data yet" :
      "stale " + Math.round((Date.now() - lastOk) / 1000) + "s";
  }
}

tick();
setInterval(tick, POLL_MS);
window.addEventListener("resize", () => tick());
</script>
</body>
</html>
)page";

const char* dashboard_page() { return kPage; }

}  // namespace bpmm
