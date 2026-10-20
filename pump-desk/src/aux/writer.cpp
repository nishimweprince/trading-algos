#include "aux/writer.hpp"

#include <sqlite3.h>

#include <chrono>
#include <cinttypes>
#include <ctime>
#include <stdexcept>
#include <thread>

#include "core/base58.hpp"
#include "core/sys.hpp"
#include "sol/constants.hpp"

namespace pd {

void Logger::line(const char* level, const std::string& m) {
  std::lock_guard<std::mutex> lk(mu_);
  int64_t ms = wall_ms();
  time_t s = time_t(ms / 1000);
  tm t;
  gmtime_r(&s, &t);
  std::fprintf(stderr, "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ %-5s %s\n", t.tm_year + 1900, t.tm_mon + 1, t.tm_mday,
               t.tm_hour, t.tm_min, t.tm_sec, int(ms % 1000), level, m.c_str());
  std::fflush(stderr);
}

const char* event_name(Ev t) {
  switch (t) {
    case Ev::Boot: return "boot";
    case Ev::CurveAdmit: return "curve_admit";
    case Ev::CurveFire: return "curve_fire";
    case Ev::Migrate: return "migrate";
    case Ev::Decision: return "decision";
    case Ev::Order: return "order";
    case Ev::SendAck: return "send_ack";
    case Ev::Fill: return "fill";
    case Ev::PositionOpen: return "position_open";
    case Ev::PositionUpdate: return "position_update";
    case Ev::PositionClose: return "position_close";
    case Ev::Gap: return "gap";
    case Ev::Kill: return "kill";
    case Ev::Stats: return "stats";
    case Ev::Warn: return "warn";
    case Ev::Route: return "route";
    case Ev::RivalTx: return "rival_tx";
  }
  return "unknown";
}

namespace {

std::string key_or_empty(const Pubkey& k) { return is_zero(k) ? "" : b58(k); }
std::string sig_or_empty(const Signature& s) {
  for (auto b : s) if (b) return b58(s);
  return "";
}

std::string escape(const char* s) {
  std::string out;
  for (; *s; ++s) {
    char c = *s;
    if (c == '"' || c == '\\') { out += '\\'; out += c; }
    else if (static_cast<unsigned char>(c) < 0x20) { char b[8]; std::snprintf(b, sizeof b, "\\u%04x", c); out += b; }
    else out += c;
  }
  return out;
}

struct J {
  std::string s = "{";
  bool first = true;
  J& raw(const char* k, const std::string& v) {
    if (!first) s += ",";
    first = false;
    s += "\"";
    s += k;
    s += "\":";
    s += v;
    return *this;
  }
  J& str(const char* k, const std::string& v) { return raw(k, "\"" + v + "\""); }
  J& i(const char* k, int64_t v) { return raw(k, std::to_string(v)); }
  J& d(const char* k, double v) {
    char b[48];
    std::snprintf(b, sizeof b, "%.9g", v);
    return raw(k, b);
  }
  J& b(const char* k, int64_t v) { return raw(k, v ? "true" : "false"); }
  std::string done() { return s + "}"; }
};

}  // namespace

std::string event_json(const Event& e) {
  J j;
  j.str("type", event_name(e.type)).i("ts", e.wall_ms).i("slot", int64_t(e.slot));
  switch (e.type) {
    case Ev::CurveAdmit:
      j.str("curve", b58(e.a)).str("mint", key_or_empty(e.b)).d("cost_sol", e.d[0]).b("mint_known", e.i[0]);
      break;
    case Ev::CurveFire:
      j.str("curve", b58(e.a)).str("mint", key_or_empty(e.b)).d("cost_sol", e.d[0]).b("keys_ready", e.i[0]);
      break;
    case Ev::Migrate:
      j.str("sig", sig_or_empty(e.sig)).str("mint", key_or_empty(e.a)).str("caller", key_or_empty(e.b))
          .b("landed", e.i[0]).b("tracked", e.i[1]).b("fired", e.i[2]).b("curve_buy_before", e.i[3]).b("pool_buy_after", e.i[4]);
      break;
    case Ev::Decision:
      j.str("verdict", escape(e.text)).str("mint", key_or_empty(e.a)).str("pool", key_or_empty(e.b))
          .d("size_sol", e.d[0]).d("pool_sol", e.d[1]).d("impact_pct", e.d[2]).d("completion_cost_sol", e.d[3])
          .d("virtual_sol", e.d[4]).i("recv_to_decide_ns", e.i[0]).i("recv_to_queued_ns", e.i[1])
          .str("source", e.i[2] == 0 ? "migrate_tx" : e.i[2] == 1 ? "pool_push" : "pool_wait_timeout").b("pool_seen", e.i[3]).i("base_res", e.i[4]).i("quote_res", e.i[5]);
      break;
    case Ev::Order:
      j.str("side", e.i[0] == 0 ? "buy" : "sell").str("sig", sig_or_empty(e.sig)).str("mint", b58(e.a)).str("pool", key_or_empty(e.b))
          .i("amount", e.i[1]).i("bound", e.i[2]).i("expected", e.i[3]).i("cu_price", e.i[4]).i("tip", e.i[5])
          .i("attempt", e.i[6]).i("recv_to_signed_ns", e.i[7]).str("reason", escape(e.text));
      if (e.i[0] == 1) j.d("mark", e.d[0]).d("gain_pct", e.d[1]).d("slippage_pct", e.d[2]);
      break;
    case Ev::SendAck:
      j.str("sig", sig_or_empty(e.sig)).str("route", e.i[0] == 0 ? "sender" : "jito").i("http", e.i[1]).b("ok", e.i[2])
          .i("signed_to_written_ns", e.i[3]).i("written_to_ack_ns", e.i[4]).str("side", e.i[5] == 0 ? "buy" : "sell")
          .b("definitive", e.i[6]).b("rate_limited", e.i[7]).str("error", escape(e.text));
      break;
    case Ev::Fill:
      j.str("side", e.i[0] == 0 ? "buy" : "sell").str("sig", sig_or_empty(e.sig)).str("mint", b58(e.a)).b("ok", e.i[1])
          .i("base_delta", e.i[2]).i("sol_delta", e.i[3]).i("sent_to_fill_ns", e.i[4]).d("price", e.d[0])
          .i("tx_index", e.i[5]).i("slots_to_land", e.i[6]).i("shred_to_seen_ns", e.i[7]).d("writes_ahead", e.d[1])
          .d("completed_to_seen_ms", e.d[2]).str("via", escape(e.text));
      if (!is_zero(e.c)) j.str("beaten_by", b58(e.c));
      break;
    case Ev::Route:
      j.str("sig", sig_or_empty(e.sig)).str("side", e.i[5] == 0 ? "buy" : "sell").str("leader_class", escape(e.text))
          .str("leader", key_or_empty(e.a)).i("cu_price", e.i[1]).i("cu_limit", e.i[2]).b("jito", e.i[3])
          .i("guards", e.i[4]).i("sender_tip", e.i[6]).b("routed", e.i[7]);
      break;
    case Ev::RivalTx:
      j.str("sig", sig_or_empty(e.sig)).str("signer", b58(e.a)).str("pool", b58(e.b)).i("tx_index", e.i[0])
          .i("cu_price", e.i[1]).i("cu_limit", e.i[2]).i("tip", e.i[3]).str("tip_to", key_or_empty(e.c))
          .str("route", e.d[0] == 1 ? "sender" : e.d[0] == 2 ? "jito" : e.d[0] == 3 ? "other" : "none")
          .i("quote_delta", e.i[4]).str("leader_class", leader_class_name(LeaderClass(e.i[5]))).str("leader", escape(e.text))
          .i("slots_after_migrate", e.i[6]).i("cu_consumed", e.i[7]).b("failed", e.d[1] >= 0.5);
      break;
    case Ev::PositionOpen:
      j.str("mint", b58(e.a)).str("curve", b58(e.b)).str("coin_creator", key_or_empty(e.c)).str("buy_sig", sig_or_empty(e.sig))
          .i("base", e.i[0]).i("sol_spent", e.i[1]).i("opened_ms", e.i[2]).d("entry_price", e.d[0]);
      break;
    case Ev::PositionUpdate:
      j.str("mint", b58(e.a)).i("base", e.i[0]).i("sol_received", e.i[1]).b("tp0_done", e.i[2]).b("tp1_done", e.i[3]);
      break;
    case Ev::PositionClose:
      j.str("mint", b58(e.a)).str("reason", escape(e.text)).i("pnl_lamports", e.i[0]).d("pnl_sol", e.d[0]).d("pnl_pct", e.d[1])
          .i("hold_ms", e.i[1]).i("sol_spent", e.i[2]).i("sol_received", e.i[3]).i("base_left", e.i[4])
          .d("entry_price", e.d[2]).d("last_mark", e.d[3]);
      break;
    case Ev::Stats:
      j.i("curves", e.i[0]).i("watches", e.i[1]).i("launches", e.i[2]).i("positions", e.i[3]).i("wallet", e.i[4])
          .i("reserved", e.i[5]).i("unresolved", e.i[6]).b("killed", e.i[7])
          .i("frames", int64_t(e.d[0])).i("parse_errors", int64_t(e.d[1])).i("reconnects", int64_t(e.d[2]))
          .i("events_dropped", int64_t(e.d[3])).b("stream_up", e.d[4] >= 0.5);
      break;
    default:
      j.str("text", escape(e.text));
  }
  return j.done();
}

Writer::Writer(const Config& cfg, Shared& sh, Logger& log) : cfg_(cfg), sh_(sh), log_(log) {}

Writer::~Writer() {
  for (sqlite3_stmt* s : {ins_event_, upsert_open_, upsert_pending_, update_pos_, close_pos_}) if (s) sqlite3_finalize(s);
  if (db_) sqlite3_close(db_);
  if (jsonl_) std::fclose(jsonl_);
}

void Writer::exec(const char* sql) {
  char* err = nullptr;
  if (sqlite3_exec(db_, sql, nullptr, nullptr, &err) != SQLITE_OK) {
    std::string m = err ? err : "?";
    sqlite3_free(err);
    throw std::runtime_error(std::string("sqlite: ") + m + " in: " + sql);
  }
}

void Writer::open() {
  if (sqlite3_open(cfg_.storage.db_path.c_str(), &db_) != SQLITE_OK)
    throw std::runtime_error("sqlite open " + cfg_.storage.db_path + ": " + sqlite3_errmsg(db_));
  exec("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;");
  exec(R"(CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY, type TEXT NOT NULL, ts INTEGER NOT NULL, slot INTEGER,
    a TEXT, b TEXT, c TEXT, sig TEXT,
    i0 INTEGER, i1 INTEGER, i2 INTEGER, i3 INTEGER, i4 INTEGER, i5 INTEGER, i6 INTEGER, i7 INTEGER,
    d0 REAL, d1 REAL, d2 REAL, d3 REAL, d4 REAL, d5 REAL, text TEXT);
  CREATE INDEX IF NOT EXISTS events_type_ts ON events(type, ts);
  CREATE TABLE IF NOT EXISTS positions(
    mint TEXT PRIMARY KEY, curve TEXT, coin_creator TEXT, token_program TEXT, cashback INTEGER, mayhem INTEGER,
    buy_sig TEXT, base_amount INTEGER, base_bought INTEGER, entry_price REAL, sol_spent INTEGER, sol_received INTEGER,
    opened_ms INTEGER, tp0_done INTEGER DEFAULT 0, tp1_done INTEGER DEFAULT 0,
    closed INTEGER DEFAULT 0, close_reason TEXT, pnl_lamports INTEGER, closed_ms INTEGER);
  CREATE VIEW IF NOT EXISTS decisions AS SELECT ts, slot, text AS verdict, a AS mint, b AS pool, d0 AS size_sol,
    d1 AS pool_sol, d2 AS impact_pct, d3 AS completion_cost_sol, d4 AS virtual_sol, i0 AS recv_to_decide_ns,
    i1 AS recv_to_queued_ns, CASE i2 WHEN 0 THEN 'migrate_tx' WHEN 1 THEN 'pool_push' ELSE 'pool_wait_timeout' END AS source FROM events WHERE type='decision';
  CREATE VIEW IF NOT EXISTS orders AS SELECT ts, slot, CASE i0 WHEN 0 THEN 'buy' ELSE 'sell' END AS side, sig, a AS mint,
    i1 AS amount, i2 AS bound, i3 AS expected, i4 AS cu_price, i5 AS tip, i6 AS attempt, i7 AS recv_to_signed_ns,
    text AS reason FROM events WHERE type='order';
  DROP VIEW IF EXISTS acks;
  CREATE VIEW acks AS SELECT ts, sig, CASE i0 WHEN 0 THEN 'sender' ELSE 'jito' END AS route, i1 AS http,
    i2 AS ok, i3 AS signed_to_written_ns, i4 AS written_to_ack_ns, i7 AS rate_limited, text AS error FROM events WHERE type='send_ack';
  DROP VIEW IF EXISTS fills;
  CREATE VIEW fills AS SELECT ts, slot, CASE i0 WHEN 0 THEN 'buy' ELSE 'sell' END AS side, sig, a AS mint,
    i1 AS ok, i2 AS base_delta, i3 AS sol_delta, i4 AS sent_to_fill_ns, d0 AS price, text AS via,
    d5 = 1 AS has_landing, i5 AS tx_index, i6 AS slots_to_land, i7 AS shred_to_seen_ns, d1 AS writes_ahead,
    d2 AS completed_to_seen_ms, c AS beaten_by FROM events WHERE type='fill';
  CREATE VIEW IF NOT EXISTS routes AS SELECT ts, slot AS target_slot, sig, CASE i5 WHEN 0 THEN 'buy' ELSE 'sell' END AS side,
    text AS leader_class, a AS leader, i1 AS cu_price, i2 AS cu_limit, i3 AS jito, i4 AS guards, i6 AS sender_tip,
    i7 AS routed FROM events WHERE type='route';
  CREATE VIEW IF NOT EXISTS rival_txs AS SELECT ts, slot, sig, a AS signer, b AS pool, i0 AS tx_index, i1 AS cu_price,
    i2 AS cu_limit, i3 AS tip, c AS tip_to, CASE CAST(d0 AS INTEGER) WHEN 1 THEN 'sender' WHEN 2 THEN 'jito' WHEN 3 THEN 'other' ELSE 'none' END AS route,
    i4 AS quote_delta, CASE i5 WHEN 1 THEN 'bam' WHEN 2 THEN 'firedancer' ELSE 'agave' END AS leader_class, text AS leader,
    i6 AS slots_after_migrate, i7 AS cu_consumed, d1 AS failed FROM events WHERE type='rival_tx';
  CREATE INDEX IF NOT EXISTS events_type_b ON events(type, b);
  CREATE VIEW IF NOT EXISTS migrations AS SELECT ts, slot, sig, a AS mint, b AS caller, i0 AS landed, i1 AS tracked,
    i2 AS fired, i3 AS curve_buy_before, i4 AS pool_buy_after FROM events WHERE type='migrate';
  )");
  auto prep = [&](const char* sql, sqlite3_stmt** s) {
    if (sqlite3_prepare_v2(db_, sql, -1, s, nullptr) != SQLITE_OK) throw std::runtime_error(sqlite3_errmsg(db_));
  };
  prep("INSERT INTO events(type,ts,slot,a,b,c,sig,i0,i1,i2,i3,i4,i5,i6,i7,d0,d1,d2,d3,d4,d5,text) "
       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", &ins_event_);
  prep("INSERT INTO positions(mint,curve,coin_creator,token_program,cashback,mayhem,buy_sig,base_amount,base_bought,"
       "entry_price,sol_spent,sol_received,opened_ms,closed) VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,0) "
       "ON CONFLICT(mint) DO UPDATE SET curve=excluded.curve, coin_creator=excluded.coin_creator, "
       "token_program=excluded.token_program, cashback=excluded.cashback, mayhem=excluded.mayhem, buy_sig=excluded.buy_sig, "
       "base_amount=excluded.base_amount, base_bought=excluded.base_bought, entry_price=excluded.entry_price, "
       "sol_spent=excluded.sol_spent, sol_received=0, opened_ms=excluded.opened_ms, tp0_done=0, tp1_done=0, closed=0, "
       "close_reason=NULL, pnl_lamports=NULL, closed_ms=NULL", &upsert_open_);
  // A buy in flight: entry_price 0 marks it pending until the fill (PositionOpen) lands.
  prep("INSERT INTO positions(mint,curve,coin_creator,token_program,cashback,mayhem,buy_sig,base_amount,base_bought,"
       "entry_price,sol_spent,sol_received,opened_ms,closed) VALUES(?,'',?,?,?,?,?,0,0,0,0,0,?,0) "
       "ON CONFLICT(mint) DO UPDATE SET coin_creator=excluded.coin_creator, token_program=excluded.token_program, "
       "cashback=excluded.cashback, mayhem=excluded.mayhem, buy_sig=excluded.buy_sig, base_amount=0, base_bought=0, "
       "entry_price=0, sol_spent=0, sol_received=0, opened_ms=excluded.opened_ms, tp0_done=0, tp1_done=0, closed=0, "
       "close_reason=NULL, pnl_lamports=NULL, closed_ms=NULL", &upsert_pending_);
  prep("UPDATE positions SET base_amount=?, sol_received=?, tp0_done=?, tp1_done=? WHERE mint=?", &update_pos_);
  prep("UPDATE positions SET closed=1, close_reason=?, pnl_lamports=?, closed_ms=?, base_amount=?, sol_received=? WHERE mint=?",
       &close_pos_);
  jsonl_ = std::fopen(cfg_.storage.jsonl_path.c_str(), "a");
  if (!jsonl_) throw std::runtime_error("cannot open " + cfg_.storage.jsonl_path);
}

std::vector<RestoredPosition> Writer::open_positions() {
  std::vector<RestoredPosition> out;
  sqlite3_stmt* s = nullptr;
  sqlite3_prepare_v2(db_, "SELECT mint,curve,coin_creator,token_program,cashback,mayhem,base_amount,base_bought,entry_price,"
                          "sol_spent,sol_received,opened_ms,tp0_done,tp1_done FROM positions WHERE closed=0", -1, &s, nullptr);
  auto txt = [&](int c) { const unsigned char* t = sqlite3_column_text(s, c); return std::string(t ? reinterpret_cast<const char*>(t) : ""); };
  while (sqlite3_step(s) == SQLITE_ROW) {
    RestoredPosition r;
    r.mint = parse_pubkey(txt(0));
    if (!txt(1).empty()) r.curve = parse_pubkey(txt(1));
    if (!txt(2).empty()) r.creator = parse_pubkey(txt(2));
    r.token_program = parse_pubkey(txt(3));
    r.cashback = sqlite3_column_int(s, 4);
    r.mayhem = sqlite3_column_int(s, 5);
    r.base_amount = uint64_t(sqlite3_column_int64(s, 6));
    r.base_bought = uint64_t(sqlite3_column_int64(s, 7));
    r.entry_price = sqlite3_column_double(s, 8);
    r.sol_spent = sqlite3_column_int64(s, 9);
    r.sol_received = sqlite3_column_int64(s, 10);
    r.opened_ms = sqlite3_column_int64(s, 11);
    r.tp0_done = sqlite3_column_int(s, 12);
    r.tp1_done = sqlite3_column_int(s, 13);
    out.push_back(r);
  }
  sqlite3_finalize(s);
  return out;
}

void Writer::close_orphan(const Pubkey& mint, const char* reason) {
  sqlite3_reset(close_pos_);
  sqlite3_bind_text(close_pos_, 1, reason, -1, SQLITE_TRANSIENT);
  sqlite3_bind_null(close_pos_, 2);
  sqlite3_bind_int64(close_pos_, 3, wall_ms());
  sqlite3_bind_int64(close_pos_, 4, 0);
  sqlite3_bind_null(close_pos_, 5);
  std::string m = b58(mint);
  sqlite3_bind_text(close_pos_, 6, m.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_step(close_pos_);
}

void Writer::write(const Event& e) {
  std::string line = event_json(e);
  std::fputs(line.c_str(), jsonl_);
  std::fputc('\n', jsonl_);

  sqlite3_stmt* s = ins_event_;
  sqlite3_reset(s);
  std::string a = key_or_empty(e.a), b = key_or_empty(e.b), c = key_or_empty(e.c), sig = sig_or_empty(e.sig);
  sqlite3_bind_text(s, 1, event_name(e.type), -1, SQLITE_STATIC);
  sqlite3_bind_int64(s, 2, e.wall_ms);
  sqlite3_bind_int64(s, 3, int64_t(e.slot));
  sqlite3_bind_text(s, 4, a.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_bind_text(s, 5, b.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_bind_text(s, 6, c.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_bind_text(s, 7, sig.c_str(), -1, SQLITE_TRANSIENT);
  for (int k = 0; k < 8; ++k) sqlite3_bind_int64(s, 8 + k, e.i[k]);
  for (int k = 0; k < 6; ++k) sqlite3_bind_double(s, 16 + k, e.d[k]);
  sqlite3_bind_text(s, 22, e.text, -1, SQLITE_TRANSIENT);
  sqlite3_step(s);

  if (e.type == Ev::Order && e.i[0] == 0) {
    sqlite3_stmt* p = upsert_pending_;
    sqlite3_reset(p);
    std::string tp = b58(e.d[3] > 0.5 ? prog::TOKEN_2022 : prog::TOKEN);
    sqlite3_bind_text(p, 1, a.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(p, 2, c.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(p, 3, tp.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_int(p, 4, e.d[4] > 0.5);
    sqlite3_bind_int(p, 5, e.d[5] > 0.5);
    sqlite3_bind_text(p, 6, sig.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_int64(p, 7, e.wall_ms);
    sqlite3_step(p);
  } else if (e.type == Ev::PositionOpen) {
    sqlite3_stmt* p = upsert_open_;
    sqlite3_reset(p);
    std::string tp = b58(e.i[5] ? prog::TOKEN_2022 : prog::TOKEN);
    sqlite3_bind_text(p, 1, a.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(p, 2, b.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(p, 3, c.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(p, 4, tp.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_int(p, 5, int(e.i[3]));
    sqlite3_bind_int(p, 6, int(e.i[4]));
    sqlite3_bind_text(p, 7, sig.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_int64(p, 8, e.i[0]);
    sqlite3_bind_int64(p, 9, e.i[0]);
    sqlite3_bind_double(p, 10, e.d[0]);
    sqlite3_bind_int64(p, 11, e.i[1]);
    sqlite3_bind_int64(p, 12, e.i[2]);
    sqlite3_step(p);
  } else if (e.type == Ev::PositionUpdate) {
    sqlite3_stmt* p = update_pos_;
    sqlite3_reset(p);
    sqlite3_bind_int64(p, 1, e.i[0]);
    sqlite3_bind_int64(p, 2, e.i[1]);
    sqlite3_bind_int(p, 3, int(e.i[2]));
    sqlite3_bind_int(p, 4, int(e.i[3]));
    sqlite3_bind_text(p, 5, a.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_step(p);
  } else if (e.type == Ev::PositionClose) {
    sqlite3_stmt* p = close_pos_;
    sqlite3_reset(p);
    sqlite3_bind_text(p, 1, e.text, -1, SQLITE_TRANSIENT);
    sqlite3_bind_int64(p, 2, e.i[0]);
    sqlite3_bind_int64(p, 3, e.wall_ms);
    sqlite3_bind_int64(p, 4, e.i[4]);
    sqlite3_bind_int64(p, 5, e.i[3]);
    sqlite3_bind_text(p, 6, a.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_step(p);
  }
  if (e.type == Ev::Stats) status_line(e);
  else if (e.type != Ev::CurveAdmit && e.type != Ev::Migrate && e.type != Ev::SendAck && e.type != Ev::RivalTx &&
           e.type != Ev::Route)
    log_.info(line);
  else if (e.type == Ev::SendAck && e.i[2] == 0) log_.warn(line);
}

void Writer::status_line(const Event& e) {
  const Counters& c = sh_.counters;
  char buf[512];
  std::snprintf(buf, sizeof buf,
                "status slot=%" PRIu64 " stream=%s frames=%" PRIu64 " parse_err=%" PRIu64 " reconnects=%" PRIu64
                " curves=%" PRId64 " fires=%" PRIu64 " migrates=%" PRIu64 " triggers=%" PRIu64 " vetoes=%" PRIu64
                " buys=%" PRIu64 " sells=%" PRIu64 " positions=%" PRId64 " wallet=%.4f reserved=%.4f unresolved=%" PRId64
                " ev_drop=%" PRIu64 " killed=%d",
                e.slot, c.stream_up.load() ? "up" : "DOWN", c.frames.load(), c.parse_errors.load(), c.reconnects.load(),
                e.i[0], c.fires.load(), c.migrates.load(), c.triggers.load(), c.vetoes.load(), c.buys.load(),
                c.sells.load(), e.i[3], double(e.i[4]) / 1e9, double(e.i[5]) / 1e9, e.i[6], c.events_dropped.load(),
                int(e.i[7]));
  log_.info(buf);
}

void Writer::run() {
  name_thread("pd-writer");
  pin_thread(cfg_.threads.aux_cpu);
  for (;;) {
    bool stopping = sh_.stop.load(std::memory_order_relaxed);
    int n = 0;
    Event e;
    exec("BEGIN");
    while (n < 2000 && sh_.events.try_pop(e)) {
      write(e);
      ++n;
    }
    exec("COMMIT");
    if (n) std::fflush(jsonl_);
    if (stopping && n == 0) break;
    if (n == 0) std::this_thread::sleep_for(std::chrono::milliseconds(20));
  }
}

}  // namespace pd
