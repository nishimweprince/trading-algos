// pump-desk-status: a read-only page over the run database.
//
//   pump-desk-status [data/pump-desk.db] [--port 8787]
//
// Binds 127.0.0.1 only. Each request opens the database, reads, and closes,
// so a browser or a tunnel cannot stall the trading threads. The page polls
// /api/status every 10 seconds, which is how often the writer stores a stats row.
//
// Live health (stream, frames, reconnects, parse errors, drops) is the latest
// stats row. Trade counts and PnL are a bounded window over the events index.
#include <sqlite3.h>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <string>
#include <string_view>
#include <vector>

namespace {

constexpr int kDefaultPort = 8787;
constexpr int64_t kFreshMs = 25000;

struct Db {
  sqlite3* p = nullptr;
  ~Db() {
    if (p) sqlite3_close(p);
  }
};

struct Stmt {
  sqlite3_stmt* s = nullptr;
  ~Stmt() {
    if (s) sqlite3_finalize(s);
  }
};

struct Js {
  std::string s = "{";
  bool first = true;
  void key(const char* k) {
    if (!first) s += ',';
    first = false;
    s += '"';
    s += k;
    s += "\":";
  }
  void n(const char* k, int64_t v) { key(k); s += std::to_string(v); }
  void f(const char* k, double v) {
    key(k);
    if (!std::isfinite(v)) {
      s += "null";
      return;
    }
    char b[64];
    std::snprintf(b, sizeof b, "%.8g", v);
    s += b;
  }
  void b(const char* k, bool v) { key(k); s += v ? "true" : "false"; }
  void str(const char* k, const std::string& v);
  void raw(const char* k, const std::string& json) { key(k); s += json; }
  void nul(const char* k) { key(k); s += "null"; }
  std::string done() const { return s + "}"; }
};

std::string escape(std::string_view in) {
  std::string out;
  out.reserve(in.size());
  for (unsigned char c : in) {
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\b': out += "\\b"; break;
      case '\f': out += "\\f"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if (c < 0x20) {
          char b[8];
          std::snprintf(b, sizeof b, "\\u%04x", c);
          out += b;
        } else {
          out += char(c);
        }
    }
  }
  return out;
}

void Js::str(const char* k, const std::string& v) {
  key(k);
  s += '"';
  s += escape(v);
  s += '"';
}

int64_t wall_ms() { return int64_t(std::time(nullptr)) * 1000; }

int64_t col_i(sqlite3_stmt* s, int c) {
  if (sqlite3_column_type(s, c) == SQLITE_NULL) return 0;
  return sqlite3_column_int64(s, c);
}

double col_d(sqlite3_stmt* s, int c) {
  if (sqlite3_column_type(s, c) == SQLITE_NULL) return 0;
  return sqlite3_column_double(s, c);
}

std::string col_s(sqlite3_stmt* s, int c) {
  const unsigned char* t = sqlite3_column_text(s, c);
  return t ? reinterpret_cast<const char*>(t) : "";
}

bool prepare(sqlite3* db, const char* sql, Stmt& st, std::string& err) {
  if (sqlite3_prepare_v2(db, sql, -1, &st.s, nullptr) != SQLITE_OK) {
    err = sqlite3_errmsg(db);
    return false;
  }
  return true;
}

// p50 / p99 over a capped, already-fetched sample. Same index rule as pump-desk-report.
void percentiles(std::vector<double> v, double scale, int64_t& n, double& p50, double& p99) {
  n = int64_t(v.size());
  if (v.empty()) return;
  std::sort(v.begin(), v.end());
  auto q = [&](double p) { return v[std::min(v.size() - 1, size_t(p * double(v.size())))] * scale; };
  p50 = q(0.5);
  p99 = q(0.99);
}

std::string pct_json(int64_t n, double p50, double p99) {
  Js j;
  j.n("n", n);
  if (n) {
    j.f("p50", p50);
    j.f("p99", p99);
  } else {
    j.nul("p50");
    j.nul("p99");
  }
  return j.done();
}

bool load_sample(sqlite3* db, const char* sql, int64_t since, std::vector<double>& out, std::string& err) {
  Stmt st;
  if (!prepare(db, sql, st, err)) return false;
  sqlite3_bind_int64(st.s, 1, since);
  int rc;
  while ((rc = sqlite3_step(st.s)) == SQLITE_ROW) out.push_back(sqlite3_column_double(st.s, 0));
  if (rc != SQLITE_DONE) {
    err = sqlite3_errmsg(db);
    return false;
  }
  return true;
}

// hours is clamped to 1..168. Missing or junk query values stay at 24.
int hours_of(std::string_view query) {
  constexpr int kDefault = 24;
  auto k = query.find("hours=");
  if (k == std::string_view::npos) return kDefault;
  k += 6;
  if (k >= query.size() || query[k] < '0' || query[k] > '9') return kDefault;
  int v = 0;
  for (; k < query.size() && query[k] >= '0' && query[k] <= '9'; ++k) {
    v = v * 10 + (query[k] - '0');
    if (v > 168) return 168;
  }
  return v < 1 ? kDefault : v;
}

std::string status_json(const std::string& path, int hours, int& http) {
  http = 200;
  Db db;
  if (sqlite3_open_v2(path.c_str(), &db.p, SQLITE_OPEN_READONLY, nullptr) != SQLITE_OK) {
    http = 503;
    Js j;
    j.b("ok", false);
    j.str("error", db.p ? sqlite3_errmsg(db.p) : "cannot open database");
    return j.done();
  }
  sqlite3_exec(db.p, "PRAGMA query_only=ON; PRAGMA busy_timeout=200;", nullptr, nullptr, nullptr);

  const int64_t now = wall_ms();
  const int64_t since = now - int64_t(hours) * 3600 * 1000;
  std::string err;

  Stmt live;
  if (!prepare(db.p,
               "SELECT ts, slot, i0, i1, i2, i3, i4, i5, i6, i7, d0, d1, d2, d3, d4 "
               "FROM events WHERE type='stats' ORDER BY ts DESC LIMIT 1",
               live, err)) {
    http = 500;
    Js j;
    j.b("ok", false);
    j.str("error", err);
    return j.done();
  }
  std::string live_json = "null";
  int live_rc = sqlite3_step(live.s);
  if (live_rc != SQLITE_ROW && live_rc != SQLITE_DONE) {
    http = live_rc == SQLITE_BUSY ? 503 : 500;
    Js j;
    j.b("ok", false);
    j.str("error", sqlite3_errmsg(db.p));
    return j.done();
  }
  if (live_rc == SQLITE_ROW) {
    int64_t stats_ts = col_i(live.s, 0);
    int64_t age = now - stats_ts;
    if (age < 0) age = 0;
    Js j;
    j.n("stats_ts", stats_ts);
    j.n("age_ms", age);
    j.b("fresh", stats_ts > 0 && age < kFreshMs);
    j.b("stream_up", col_d(live.s, 14) >= 0.5);
    j.n("slot", col_i(live.s, 1));
    j.n("frames", int64_t(col_d(live.s, 10)));
    j.n("parse_errors", int64_t(col_d(live.s, 11)));
    j.n("reconnects", int64_t(col_d(live.s, 12)));
    j.n("events_dropped", int64_t(col_d(live.s, 13)));
    j.b("killed", col_i(live.s, 9) != 0);
    j.n("curves", col_i(live.s, 2));
    j.n("watches", col_i(live.s, 3));
    j.n("launches", col_i(live.s, 4));
    j.n("positions", col_i(live.s, 5));
    j.n("wallet_lamports", col_i(live.s, 6));
    j.n("reserved_lamports", col_i(live.s, 7));
    j.n("unresolved", col_i(live.s, 8));
    live_json = j.done();
  }

  // Indexed counts on events(type, ts). Decisions whose verdict is not "buy"
  // are the vetoes, including screen rejects and the not-fired path.
  Stmt counts;
  if (!prepare(db.p,
               "SELECT"
               " (SELECT COUNT(*) FROM events WHERE type='curve_fire' AND ts>=?1),"
               " (SELECT COUNT(*) FROM events WHERE type='migrate' AND ts>=?1),"
               " (SELECT COUNT(*) FROM events WHERE type='decision' AND ts>=?1),"
               " (SELECT COUNT(*) FROM events WHERE type='decision' AND IFNULL(text,'')!='buy' AND ts>=?1),"
               " (SELECT COUNT(*) FROM events WHERE type='order' AND i0=0 AND ts>=?1),"
               " (SELECT COUNT(*) FROM events WHERE type='order' AND i0=1 AND ts>=?1),"
               " (SELECT COUNT(*) FROM events WHERE type='position_close' AND ts>=?1),"
               " (SELECT COALESCE(SUM(i0 > 0), 0) FROM events WHERE type='position_close' AND ts>=?1),"
               " (SELECT COALESCE(SUM(i0), 0) FROM events WHERE type='position_close' AND ts>=?1),"
               " (SELECT AVG(d1) FROM events WHERE type='position_close' AND ts>=?1)",
               counts, err)) {
    http = 500;
    Js j;
    j.b("ok", false);
    j.str("error", err);
    return j.done();
  }
  sqlite3_bind_int64(counts.s, 1, since);
  int count_rc = sqlite3_step(counts.s);
  if (count_rc != SQLITE_ROW) {
    http = count_rc == SQLITE_BUSY ? 503 : 500;
    Js j;
    j.b("ok", false);
    j.str("error", sqlite3_errmsg(db.p));
    return j.done();
  }
  const int64_t closed = col_i(counts.s, 6);
  const bool have_avg = sqlite3_column_type(counts.s, 9) != SQLITE_NULL;

  std::vector<double> decide, buy_fill;
  if (!load_sample(db.p,
                   "SELECT i0 FROM events WHERE type='decision' AND text='buy' AND ts>=?1 "
                   "ORDER BY ts DESC LIMIT 200",
                   since, decide, err) ||
      !load_sample(db.p,
                   "SELECT i4 FROM events WHERE type='fill' AND i0=0 AND i1=1 AND ts>=?1 "
                   "ORDER BY ts DESC LIMIT 200",
                   since, buy_fill, err)) {
    http = 500;
    Js j;
    j.b("ok", false);
    j.str("error", err);
    return j.done();
  }
  int64_t decide_n = 0, fill_n = 0;
  double decide_p50 = 0, decide_p99 = 0, fill_p50 = 0, fill_p99 = 0;
  percentiles(std::move(decide), 1e-3, decide_n, decide_p50, decide_p99);
  percentiles(std::move(buy_fill), 1e-6, fill_n, fill_p50, fill_p99);

  Js window;
  window.n("fires", col_i(counts.s, 0));
  window.n("migrates", col_i(counts.s, 1));
  window.n("decisions", col_i(counts.s, 2));
  window.n("vetoes", col_i(counts.s, 3));
  window.n("buys", col_i(counts.s, 4));
  window.n("sells", col_i(counts.s, 5));
  window.n("closed", closed);
  window.n("wins", col_i(counts.s, 7));
  window.n("net_lamports", col_i(counts.s, 8));
  if (have_avg) window.f("avg_pnl_pct", col_d(counts.s, 9));
  else window.nul("avg_pnl_pct");
  window.raw("decide_us", pct_json(decide_n, decide_p50, decide_p99));
  window.raw("buy_fill_ms", pct_json(fill_n, fill_p50, fill_p99));

  Stmt open;
  if (!prepare(db.p,
               "SELECT mint, base_amount, entry_price, opened_ms FROM positions "
               "WHERE closed=0 ORDER BY opened_ms DESC LIMIT 32",
               open, err)) {
    http = 500;
    Js j;
    j.b("ok", false);
    j.str("error", err);
    return j.done();
  }
  std::string open_json = "[";
  bool first = true;
  int open_rc;
  while ((open_rc = sqlite3_step(open.s)) == SQLITE_ROW) {
    if (!first) open_json += ',';
    first = false;
    Js row;
    row.str("mint", col_s(open.s, 0));
    row.n("base", col_i(open.s, 1));
    row.f("entry", col_d(open.s, 2));
    row.n("opened_ms", col_i(open.s, 3));
    open_json += row.done();
  }
  if (open_rc != SQLITE_DONE) {
    http = open_rc == SQLITE_BUSY ? 503 : 500;
    Js j;
    j.b("ok", false);
    j.str("error", sqlite3_errmsg(db.p));
    return j.done();
  }
  open_json += ']';

  Stmt errs;
  if (!prepare(db.p,
               "SELECT ts, CASE i0 WHEN 0 THEN 'sender' ELSE 'jito' END, substr(IFNULL(text,''), 1, 160) "
               "FROM events WHERE type='send_ack' AND i2=0 AND ts>=?1 ORDER BY ts DESC LIMIT 8",
               errs, err)) {
    http = 500;
    Js j;
    j.b("ok", false);
    j.str("error", err);
    return j.done();
  }
  sqlite3_bind_int64(errs.s, 1, since);
  std::string err_json = "[";
  first = true;
  int err_rc;
  while ((err_rc = sqlite3_step(errs.s)) == SQLITE_ROW) {
    if (!first) err_json += ',';
    first = false;
    Js row;
    row.n("ts", col_i(errs.s, 0));
    row.str("route", col_s(errs.s, 1));
    row.str("error", col_s(errs.s, 2));
    err_json += row.done();
  }
  if (err_rc != SQLITE_DONE) {
    http = err_rc == SQLITE_BUSY ? 503 : 500;
    Js j;
    j.b("ok", false);
    j.str("error", sqlite3_errmsg(db.p));
    return j.done();
  }
  err_json += ']';

  Js out;
  out.b("ok", true);
  out.n("hours", hours);
  out.n("now_ms", now);
  out.raw("live", live_json);
  out.raw("window", window.done());
  out.raw("open", open_json);
  out.raw("errors", err_json);
  return out.done();
}

const char kPage[] = R"page(<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pump-desk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,400..700&display=swap" rel="stylesheet">
<style>
  :root {
    --paper: #e7efe6;
    --ink: #132018;
    --muted: #3d4a42;
    --gain: #165c32;
    --loss: #8f2d2a;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--paper);
    color: var(--ink);
    font-family: "DM Sans", ui-sans-serif, system-ui, sans-serif;
    font-size: 17px;
    line-height: 1.35;
  }
  main { max-width: 44rem; margin: 0 auto; padding: 1.5rem 1.25rem 3rem; }
  h1 { font-size: 1rem; font-weight: 600; margin: 0 0 0.35rem; }
  h2 { font-size: 1.15rem; font-weight: 600; margin: 0 0 0.6rem; }
  main > section { margin-top: 2rem; }
  #stream { margin: 0; font-size: 1.15rem; }
  #stream.bad { color: var(--loss); }
  .hero {
    font-size: 2.75rem;
    line-height: 1.05;
    margin: 1.25rem 0 0;
    font-variant-numeric: tabular-nums;
  }
  @media (max-width: 400px) {
    .hero { font-size: 2.15rem; }
  }
  .sub { margin: 0.2rem 0 0; color: var(--muted); font-variant-numeric: tabular-nums; }
  .net { font-size: 1.35rem; margin: 0.85rem 0 0; font-variant-numeric: tabular-nums; }
  .net.up { color: var(--gain); }
  .net.down { color: var(--loss); }
  .wins { margin: 0.15rem 0 0; color: var(--muted); }
  .cols {
    display: grid;
    grid-template-columns: 1fr;
    gap: 1.75rem;
    margin-top: 1.75rem;
  }
  @media (min-width: 720px) {
    .cols { grid-template-columns: 1fr 1fr; gap: 2.5rem; }
  }
  dl {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 0.35rem 1.25rem;
    margin: 0;
  }
  dt { color: var(--muted); }
  dd { margin: 0; text-align: right; font-variant-numeric: tabular-nums; }
  table { width: 100%; border-collapse: collapse; }
  td { padding: 0.35rem 0.75rem 0.35rem 0; vertical-align: top; font-variant-numeric: tabular-nums; }
  td:last-child { padding-right: 0; text-align: right; }
  .mint { word-break: break-all; }
  .err { list-style: none; margin: 0; padding: 0; }
  .err li { margin: 0 0 0.55rem; }
  .err time { color: var(--muted); }
  .empty { margin: 0; color: var(--muted); }
  footer { margin-top: 2rem; color: var(--muted); font-size: 0.95rem; }
  footer p { margin: 0.2rem 0; }
</style>
</head>
<body>
<main>
  <header>
    <h1>pump-desk</h1>
    <p id="stream" aria-live="polite">Waiting for the first reading</p>
  </header>
  <p id="wallet" class="hero">—</p>
  <p id="reserved" class="sub"></p>
  <p id="net" class="net">—</p>
  <p id="wins" class="wins"></p>
  <div class="cols">
    <section>
      <h2>Right now</h2>
      <dl id="live"></dl>
    </section>
    <section>
      <h2 id="window-title">Last 24 hours</h2>
      <dl id="window"></dl>
    </section>
  </div>
  <section>
    <h2>Open positions</h2>
    <div id="positions"></div>
  </section>
  <section>
    <h2>Send errors</h2>
    <div id="errors"></div>
  </section>
  <footer>
    <p>Refreshes every 10 seconds. This page only reads the run database.</p>
    <p>Stream, frames, reconnects, parse errors, and drops count from process start. Trade counts and the result are the window above.</p>
  </footer>
</main>
<script>
const EVERY = 10000;
let busy = false;

function sol(lamports, signed) {
  const n = Number(lamports) / 1e9;
  const body = Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 4 });
  if (!signed) return body + " SOL";
  if (n > 0) return "+" + body + " SOL";
  if (n < 0) return "\u2212" + body + " SOL";
  return body + " SOL";
}
function num(n) { return Number(n).toLocaleString(); }
function age(ms) {
  const s = Math.max(0, Math.round(Number(ms) / 1000));
  if (s < 60) return s + "s ago";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m ago";
  return Math.floor(m / 60) + "h ago";
}
function fill(dl, pairs) {
  dl.replaceChildren();
  for (const [k, v] of pairs) {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v;
    dl.append(dt, dd);
  }
}
function lat(sample, unit) {
  if (!sample || !sample.n) return "n=0";
  const p50 = Number(sample.p50).toLocaleString(undefined, { maximumFractionDigits: 1 });
  const p99 = Number(sample.p99).toLocaleString(undefined, { maximumFractionDigits: 1 });
  return "p50 " + p50 + " " + unit + ", p99 " + p99 + " (n=" + num(sample.n) + ")";
}
function setStream(text, bad) {
  const el = document.getElementById("stream");
  el.textContent = text;
  el.classList.toggle("bad", !!bad);
}
function render(j) {
  const hours = j.hours || 24;
  document.getElementById("window-title").textContent = "Last " + hours + (hours === 1 ? " hour" : " hours");
  const w = j.window || {};
  const net = document.getElementById("net");
  net.textContent = sol(w.net_lamports || 0, true) + " over " + hours + (hours === 1 ? " hour" : " hours");
  net.classList.toggle("up", Number(w.net_lamports) > 0);
  net.classList.toggle("down", Number(w.net_lamports) < 0);
  const wins = document.getElementById("wins");
  if (!w.closed) wins.textContent = "No closed trades in this window";
  else {
    const nWins = Number(w.wins) || 0;
    let line = num(nWins) + (nWins === 1 ? " win of " : " wins of ") + num(w.closed);
    if (w.avg_pnl_pct !== null && w.avg_pnl_pct !== undefined) {
      const pct = Number(w.avg_pnl_pct);
      const body = Math.abs(pct).toLocaleString(undefined, { maximumFractionDigits: 2 });
      line += ", average " + (pct > 0 ? "+" : pct < 0 ? "\u2212" : "") + body + "%";
    }
    wins.textContent = line;
  }
  const live = j.live;
  const wallet = document.getElementById("wallet");
  const reserved = document.getElementById("reserved");
  if (!live) {
    setStream("No stats yet. A line is written every 10 seconds once pump-desk is running.", true);
    wallet.textContent = "\u2014";
    reserved.textContent = "";
    fill(document.getElementById("live"), []);
  } else {
    const bad = !live.fresh || !live.stream_up || live.killed;
    let line;
    if (!live.fresh) line = "Last stats " + age(live.age_ms) + ". pump-desk may be stopped.";
    else line = (live.stream_up ? "Stream up" : "Stream down") + ", stats " + age(live.age_ms);
    if (live.killed) line += ". Entries stopped";
    setStream(line, bad);
    wallet.textContent = sol(live.wallet_lamports, false);
    reserved.textContent = sol(live.reserved_lamports, false) + " reserved";
    fill(document.getElementById("live"), [
      ["Slot", num(live.slot)],
      ["Curves", num(live.curves)],
      ["Open positions", num(live.positions)],
      ["Unresolved curves", num(live.unresolved)],
      ["Frames", num(live.frames)],
      ["Reconnects", num(live.reconnects)],
      ["Parse errors", num(live.parse_errors)],
      ["Event drops", num(live.events_dropped)]
    ]);
  }
  fill(document.getElementById("window"), [
    ["Fires", num(w.fires || 0)],
    ["Migrates", num(w.migrates || 0)],
    ["Decisions", num(w.decisions || 0)],
    ["Vetoes", num(w.vetoes || 0)],
    ["Buys", num(w.buys || 0)],
    ["Sells", num(w.sells || 0)],
    ["Decide", lat(w.decide_us, "\u00b5s")],
    ["Buy fill", lat(w.buy_fill_ms, "ms")]
  ]);
  const pos = document.getElementById("positions");
  pos.replaceChildren();
  const rows = j.open || [];
  if (!rows.length) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = "None open";
    pos.append(p);
  } else {
    const table = document.createElement("table");
    for (const r of rows) {
      const tr = document.createElement("tr");
      const mint = document.createElement("td");
      mint.className = "mint";
      mint.textContent = r.mint || "\u2014";
      const meta = document.createElement("td");
      meta.textContent = Number(r.entry) > 0
        ? Number(r.entry).toLocaleString(undefined, { maximumFractionDigits: 8 })
        : "pending";
      const when = document.createElement("td");
      when.textContent = r.opened_ms ? new Date(r.opened_ms).toLocaleString() : "";
      tr.append(mint, meta, when);
      table.append(tr);
    }
    pos.append(table);
  }
  const box = document.getElementById("errors");
  box.replaceChildren();
  const errs = j.errors || [];
  if (!errs.length) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = "None in this window";
    box.append(p);
  } else {
    const ul = document.createElement("ul");
    ul.className = "err";
    for (const e of errs) {
      const li = document.createElement("li");
      const time = document.createElement("time");
      time.dateTime = new Date(e.ts).toISOString();
      time.textContent = e.ts ? new Date(e.ts).toLocaleString() : "";
      li.append(time, document.createTextNode(" " + (e.route || "") + " " + (e.error || "(empty)")));
      ul.append(li);
    }
    box.append(ul);
  }
}

async function tick() {
  if (busy) return;
  busy = true;
  try {
    const r = await fetch("/api/status?hours=24", { cache: "no-store" });
    const j = await r.json();
    if (!j.ok) setStream(j.error || "Cannot read the database", true);
    else render(j);
  } catch (e) {
    setStream("The status page did not respond.", true);
  } finally {
    busy = false;
  }
}
tick();
setInterval(tick, EVERY);
</script>
</body>
</html>
)page";

bool send_all(int fd, const char* p, size_t n) {
  while (n) {
#ifdef MSG_NOSIGNAL
    ssize_t w = ::send(fd, p, n, MSG_NOSIGNAL);
#else
    ssize_t w = ::send(fd, p, n, 0);
#endif
    if (w < 0) {
      if (errno == EINTR) continue;
      return false;
    }
    p += w;
    n -= size_t(w);
  }
  return true;
}

const char* reason(int code) {
  switch (code) {
    case 200: return "OK";
    case 204: return "No Content";
    case 400: return "Bad Request";
    case 404: return "Not Found";
    case 405: return "Method Not Allowed";
    case 503: return "Service Unavailable";
    default: return "Error";
  }
}

void respond(int fd, int code, const char* type, std::string_view body) {
  char head[320];
  int n = std::snprintf(head, sizeof head,
                        "HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %zu\r\n"
                        "Cache-Control: no-store\r\nConnection: close\r\n\r\n",
                        code, reason(code), type, body.size());
  if (n < 0 || size_t(n) >= sizeof head) return;
  if (!send_all(fd, head, size_t(n))) return;
  if (!body.empty()) send_all(fd, body.data(), body.size());
}

void serve(int fd, const std::string& path) {
  timeval tv{};
  tv.tv_sec = 2;
  ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
  ::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);

  std::string req;
  char buf[1024];
  while (req.size() < 8192 && req.find("\r\n\r\n") == std::string::npos) {
    ssize_t r = ::recv(fd, buf, sizeof buf, 0);
    if (r == 0) return;
    if (r < 0) {
      if (errno == EINTR) continue;
      return;
    }
    req.append(buf, buf + r);
  }
  if (req.size() < 5) return;
  auto line_end = req.find("\r\n");
  if (line_end == std::string::npos) {
    respond(fd, 400, "text/plain; charset=utf-8", "bad request\n");
    return;
  }
  std::string_view line(req.data(), line_end);
  auto sp1 = line.find(' ');
  auto sp2 = sp1 == std::string_view::npos ? std::string_view::npos : line.find(' ', sp1 + 1);
  if (sp1 == std::string_view::npos || sp2 == std::string_view::npos) {
    respond(fd, 400, "text/plain; charset=utf-8", "bad request\n");
    return;
  }
  std::string_view method = line.substr(0, sp1);
  std::string_view target = line.substr(sp1 + 1, sp2 - sp1 - 1);
  if (method != "GET") {
    respond(fd, 405, "text/plain; charset=utf-8", "method not allowed\n");
    return;
  }
  std::string_view q;
  auto qpos = target.find('?');
  std::string_view pth = qpos == std::string_view::npos ? target : target.substr(0, qpos);
  if (qpos != std::string_view::npos) q = target.substr(qpos + 1);

  if (pth == "/favicon.ico") {
    respond(fd, 204, "text/plain", "");
    return;
  }
  if (pth == "/") {
    respond(fd, 200, "text/html; charset=utf-8", kPage);
    return;
  }
  if (pth == "/api/status") {
    int http = 200;
    std::string body = status_json(path, hours_of(q), http);
    respond(fd, http, "application/json; charset=utf-8", body);
    return;
  }
  respond(fd, 404, "text/plain; charset=utf-8", "not found\n");
}

void usage(const char* argv0) {
  std::fprintf(stderr, "usage: %s [data/pump-desk.db] [--port %d]\n", argv0, kDefaultPort);
  std::exit(2);
}

}  // namespace

int main(int argc, char** argv) {
  std::string path = "data/pump-desk.db";
  int port = kDefaultPort;
  bool path_set = false;
  for (int i = 1; i < argc; ++i) {
    if (!std::strcmp(argv[i], "--port") && i + 1 < argc) {
      port = std::atoi(argv[++i]);
    } else if (argv[i][0] == '-') {
      usage(argv[0]);
    } else if (path_set) {
      usage(argv[0]);
    } else {
      path = argv[i];
      path_set = true;
    }
  }
  if (port < 1 || port > 65535) usage(argv[0]);

  std::signal(SIGPIPE, SIG_IGN);

#ifdef SOCK_CLOEXEC
  int fd = ::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
#else
  int fd = ::socket(AF_INET, SOCK_STREAM, 0);
#endif
  if (fd < 0) {
    std::perror("socket");
    return 1;
  }
  int yes = 1;
  ::setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof yes);
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(uint16_t(port));
  if (::inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr) != 1) {
    std::fprintf(stderr, "inet_pton failed\n");
    return 1;
  }
  if (::bind(fd, reinterpret_cast<sockaddr*>(&addr), sizeof addr) != 0) {
    std::perror("bind");
    return 1;
  }
  if (::listen(fd, 16) != 0) {
    std::perror("listen");
    return 1;
  }
  std::fprintf(stderr, "pump-desk-status listening on 127.0.0.1:%d db %s\n", port, path.c_str());
  for (;;) {
    int c = ::accept(fd, nullptr, nullptr);
    if (c < 0) {
      if (errno == EINTR) continue;
      std::fprintf(stderr, "accept: %s\n", std::strerror(errno));
      continue;
    }
    serve(c, path);
    ::close(c);
  }
}
