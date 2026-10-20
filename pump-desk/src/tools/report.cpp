// pump-desk-report: net PnL, landing and latency from the run database.
//
//   pump-desk-report [data/pump-desk.db] [--since-hours N]
//   pump-desk-report [data/pump-desk.db] --rivals [--out data/rivals.json] [--top 3] [--min-n 5] [--since-hours 72]
//
// --rivals ranks the wallets whose pool txs landed ahead of our buys and writes
// the file pump-desk reads for the rival guard and the beat_rivals CU price.
#include <sqlite3.h>

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <map>
#include <set>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <string>
#include <vector>

namespace {

sqlite3* db = nullptr;
long long since_ms = 0;

std::vector<double> column(const char* sql) {
  std::vector<double> out;
  sqlite3_stmt* s = nullptr;
  if (sqlite3_prepare_v2(db, sql, -1, &s, nullptr) != SQLITE_OK) {
    std::fprintf(stderr, "query failed: %s\n", sqlite3_errmsg(db));
    return out;
  }
  sqlite3_bind_int64(s, 1, since_ms);
  while (sqlite3_step(s) == SQLITE_ROW) out.push_back(sqlite3_column_double(s, 0));
  sqlite3_finalize(s);
  return out;
}

void percentiles(const char* label, std::vector<double> v, double scale, const char* unit) {
  if (v.empty()) {
    std::printf("  %-34s n=0\n", label);
    return;
  }
  std::sort(v.begin(), v.end());
  auto q = [&](double p) { return v[std::min(v.size() - 1, size_t(p * double(v.size())))] * scale; };
  std::printf("  %-34s n=%-5zu p50 %9.1f  p90 %9.1f  p99 %9.1f  max %9.1f %s\n", label, v.size(), q(0.5), q(0.9), q(0.99),
              v.back() * scale, unit);
}

void rows(const char* title, const char* sql) {
  std::printf("%s\n", title);
  sqlite3_stmt* s = nullptr;
  if (sqlite3_prepare_v2(db, sql, -1, &s, nullptr) != SQLITE_OK) {
    std::fprintf(stderr, "query failed: %s\n", sqlite3_errmsg(db));
    return;
  }
  sqlite3_bind_int64(s, 1, since_ms);
  while (sqlite3_step(s) == SQLITE_ROW) {
    std::printf("  ");
    for (int c = 0; c < sqlite3_column_count(s); ++c) {
      const unsigned char* t = sqlite3_column_text(s, c);
      std::printf("%-22s", t ? reinterpret_cast<const char*>(t) : "-");
    }
    std::printf("\n");
  }
  sqlite3_finalize(s);
}

const char* CLASS_NAMES[3] = {"agave", "bam", "firedancer"};

// Rival pool txs (buys that moved SOL in) that landed before our buy in the same pool: an earlier
// slot, or the same slot at a lower index. A buy with no stream landing counts every rival buy in
// the window. One row per (rival tx, our buy).
const char* AHEAD_SQL =
    "SELECT r.a, r.b, r.i1, r.i5, CAST(r.d0 AS INTEGER), r.i3 FROM events r"
    " JOIN events o ON o.type='order' AND o.i0=0 AND o.b=r.b"
    " LEFT JOIN events f ON f.type='fill' AND f.i0=0 AND f.sig=o.sig AND f.d5=1 AND f.i6>=0"
    " WHERE r.type='rival_tx' AND r.i4>0 AND r.d1<0.5 AND r.ts >= ?1"
    " AND (f.id IS NULL OR r.slot < f.slot OR (r.slot = f.slot AND r.i0 >= 0 AND f.i5 >= 0 AND r.i0 < f.i5))";

struct RivalStat {
  std::set<std::string> pools;
  long n = 0;
  long routes[4] = {0};
  std::vector<double> cu_price;
};

double quantile(std::vector<double> v, double p) {
  if (v.empty()) return 0;
  std::sort(v.begin(), v.end());
  return v[std::min(v.size() - 1, size_t(p * double(v.size())))];
}

int write_rivals(const std::string& out, int top, int min_n) {
  std::map<std::string, RivalStat> by_wallet;
  std::vector<double> class_prices[3];
  sqlite3_stmt* s = nullptr;
  if (sqlite3_prepare_v2(db, AHEAD_SQL, -1, &s, nullptr) != SQLITE_OK) {
    std::fprintf(stderr, "query failed: %s\n", sqlite3_errmsg(db));
    return 1;
  }
  sqlite3_bind_int64(s, 1, since_ms);
  while (sqlite3_step(s) == SQLITE_ROW) {
    auto txt = [&](int c) { const unsigned char* t = sqlite3_column_text(s, c); return std::string(t ? reinterpret_cast<const char*>(t) : ""); };
    RivalStat& r = by_wallet[txt(0)];
    r.pools.insert(txt(1));
    ++r.n;
    double price = sqlite3_column_double(s, 2);
    r.cu_price.push_back(price);
    int cls = sqlite3_column_int(s, 3);
    if (cls >= 0 && cls < 3) class_prices[cls].push_back(price);
    int route = sqlite3_column_int(s, 4);
    if (route >= 0 && route < 4) ++r.routes[route];
  }
  sqlite3_finalize(s);

  std::vector<std::pair<std::string, const RivalStat*>> ranked;
  for (auto& [w, r] : by_wallet) ranked.emplace_back(w, &r);
  std::sort(ranked.begin(), ranked.end(), [](auto& a, auto& b) {
    return a.second->pools.size() != b.second->pools.size() ? a.second->pools.size() > b.second->pools.size() : a.second->n > b.second->n;
  });

  std::string j = "{\n \"generated_ms\": " + std::to_string((long long)std::time(nullptr) * 1000) + ",\n \"guard\": [";
  for (int i = 0; i < top && i < int(ranked.size()); ++i) j += std::string(i ? ", " : "") + "\"" + ranked[size_t(i)].first + "\"";
  j += "],\n \"beat_cu_price\": {";
  for (int c = 0; c < 3; ++c) {
    // p90 of what landed ahead of us, + 1; too few samples: no override (the estimate rules).
    uint64_t beat = int(class_prices[c].size()) >= min_n ? uint64_t(quantile(class_prices[c], 0.9)) + 1 : 0;
    j += std::string(c ? ", " : "") + "\"" + CLASS_NAMES[c] + "\": " + std::to_string(beat);
  }
  j += "},\n \"rivals\": [";
  for (size_t i = 0; i < ranked.size() && i < 25; ++i) {
    const RivalStat& r = *ranked[i].second;
    char b[512];
    std::snprintf(b, sizeof b,
                  "%s\n  {\"wallet\": \"%s\", \"pools_ahead\": %zu, \"txs_ahead\": %ld, \"p50_cu_price\": %.0f, "
                  "\"p90_cu_price\": %.0f, \"routes\": {\"none\": %ld, \"sender\": %ld, \"jito\": %ld, \"other\": %ld}}",
                  i ? "," : "", ranked[i].first.c_str(), r.pools.size(), r.n, quantile(r.cu_price, 0.5), quantile(r.cu_price, 0.9),
                  r.routes[0], r.routes[1], r.routes[2], r.routes[3]);
    j += b;
  }
  j += "\n ]\n}\n";
  // Atomic replace: pump-desk reloads the file when its mtime changes.
  std::string tmp = out + ".tmp";
  FILE* f = std::fopen(tmp.c_str(), "w");
  if (!f || std::fputs(j.c_str(), f) < 0 || std::fclose(f) != 0 || std::rename(tmp.c_str(), out.c_str()) != 0) {
    std::fprintf(stderr, "cannot write %s\n", out.c_str());
    return 1;
  }
  std::printf("wrote %s: %zu rival wallets ahead of us, guarding %d\n", out.c_str(), ranked.size(), std::min(top, int(ranked.size())));
  for (size_t i = 0; i < ranked.size() && i < 10; ++i)
    std::printf("  %-46s pools %-4zu txs %-5ld p90 cu_price %.0f\n", ranked[i].first.c_str(), ranked[i].second->pools.size(),
                ranked[i].second->n, quantile(ranked[i].second->cu_price, 0.9));
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  std::string path = "data/pump-desk.db", rivals_out = "data/rivals.json";
  double hours = 0;
  bool rivals = false;
  int top = 3, min_n = 5;
  for (int i = 1; i < argc; ++i) {
    if (!std::strcmp(argv[i], "--since-hours") && i + 1 < argc) hours = std::atof(argv[++i]);
    else if (!std::strcmp(argv[i], "--rivals")) rivals = true;
    else if (!std::strcmp(argv[i], "--out") && i + 1 < argc) rivals_out = argv[++i];
    else if (!std::strcmp(argv[i], "--top") && i + 1 < argc) top = std::atoi(argv[++i]);
    else if (!std::strcmp(argv[i], "--min-n") && i + 1 < argc) min_n = std::atoi(argv[++i]);
    else path = argv[i];
  }
  if (sqlite3_open_v2(path.c_str(), &db, SQLITE_OPEN_READONLY, nullptr) != SQLITE_OK) {
    std::fprintf(stderr, "cannot open %s\n", path.c_str());
    return 1;
  }
  if (rivals && hours <= 0) hours = 72;  // rivals rotate: rank on recent behaviour
  if (hours > 0) since_ms = (long long)(std::time(nullptr)) * 1000 - (long long)(hours * 3600 * 1000);
  if (rivals) {
    int rc = write_rivals(rivals_out, top, min_n);
    sqlite3_close(db);
    return rc;
  }
  std::printf("pump-desk report: %s%s\n\n", path.c_str(), hours > 0 ? " (window)" : "");

  rows("Migrations (tracked / fired / landed):",
       "SELECT COUNT(*), SUM(tracked), SUM(fired), SUM(landed) FROM migrations WHERE ts >= ?1");
  rows("\nDecisions by verdict:", "SELECT verdict, COUNT(*) FROM decisions WHERE ts >= ?1 GROUP BY verdict ORDER BY 2 DESC");
  rows("\nDecisions by trigger source:", "SELECT source, COUNT(*) FROM decisions WHERE ts >= ?1 AND verdict='buy' GROUP BY source");
  rows("\nBuys: orders / filled ok / failed:",
       "SELECT (SELECT COUNT(*) FROM orders WHERE side='buy' AND ts >= ?1),"
       " (SELECT COUNT(*) FROM fills WHERE side='buy' AND ok=1 AND ts >= ?1),"
       " (SELECT COUNT(*) FROM fills WHERE side='buy' AND ok=0 AND ts >= ?1)");
  rows("\nClosed positions: n / wins / net SOL / avg % / fees+tips are inside net:",
       "SELECT COUNT(*), SUM(i0 > 0), printf('%.5f', SUM(i0) / 1e9), printf('%.2f', AVG(d1)) FROM events"
       " WHERE type='position_close' AND ts >= ?1");
  rows("\nExit reasons (n / net SOL / avg %):",
       "SELECT text, COUNT(*), printf('%.5f', SUM(i0) / 1e9), printf('%.2f', AVG(d1)) FROM events"
       " WHERE type='position_close' AND ts >= ?1 GROUP BY text ORDER BY 2 DESC");
  rows("\nOpen positions:", "SELECT mint, base_amount, printf('%.10f', entry_price), datetime(opened_ms/1000,'unixepoch')"
                            " FROM positions WHERE closed=0 AND opened_ms >= ?1");

  std::printf("\nLatency:\n");
  percentiles("frame -> decision", column("SELECT recv_to_decide_ns FROM decisions WHERE verdict='buy' AND ts >= ?1"), 1e-3, "us");
  percentiles("frame -> packet queued", column("SELECT recv_to_queued_ns FROM decisions WHERE verdict='buy' AND ts >= ?1"), 1e-3, "us");
  percentiles("packet -> written to socket", column("SELECT signed_to_written_ns FROM acks WHERE ts >= ?1"), 1e-3, "us");
  percentiles("written -> sender ack", column("SELECT written_to_ack_ns FROM acks WHERE ts >= ?1"), 1e-6, "ms");
  percentiles("buy sent -> fill seen", column("SELECT sent_to_fill_ns FROM fills WHERE side='buy' AND ts >= ?1"), 1e-6, "ms");
  percentiles("sell sent -> fill seen", column("SELECT sent_to_fill_ns FROM fills WHERE side='sell' AND ts >= ?1"), 1e-6, "ms");

  // Landing fields live on fill rows with d5=1; read events directly so an older database (old views) still works.
  std::printf("\nLanding (buys):\n");
  percentiles("slots to land", column("SELECT i6 FROM events WHERE type='fill' AND i0=0 AND i1=1 AND d5=1 AND i6>=0 AND ts >= ?1"), 1, "slots");
  percentiles("index in block", column("SELECT i5 FROM events WHERE type='fill' AND i0=0 AND i1=1 AND d5=1 AND i5>=0 AND ts >= ?1"), 1, "");
  percentiles("pool writes ahead after send", column("SELECT d1 FROM events WHERE type='fill' AND i0=0 AND i1=1 AND d5=1 AND d1>=0 AND ts >= ?1"), 1, "");
  percentiles("first shred -> fill seen", column("SELECT i7 FROM events WHERE type='fill' AND i1=1 AND d5=1 AND i7!=0 AND ts >= ?1"), 1e-6, "ms");
  percentiles("slot completed -> fill seen", column("SELECT d2 FROM events WHERE type='fill' AND i1=1 AND d5=1 AND d2!=0 AND ts >= ?1"), 1, "ms");
  rows("\nSlots to land by tip / cu_price (n / avg slots / avg index):",
       "SELECT o.i5 AS tip, o.i4 / 500000 * 500000 AS cu_price_bucket, COUNT(*), printf('%.2f', AVG(f.i6)), printf('%.1f', AVG(f.i5))"
       " FROM events f JOIN events o ON o.type='order' AND o.sig=f.sig"
       " WHERE f.type='fill' AND f.i0=0 AND f.i1=1 AND f.d5=1 AND f.i6>=0 AND f.ts >= ?1 GROUP BY 1, 2 ORDER BY 1, 2");
  rows("\nBuys by leader class (n / landed / avg slots / avg index / beaten by a guard):",
       "SELECT r.text, COUNT(*), SUM(f.i1 = 1), printf('%.2f', AVG(CASE WHEN f.i1 = 1 AND f.d5 = 1 AND f.i6 >= 0 THEN f.i6 END)),"
       " printf('%.1f', AVG(CASE WHEN f.i1 = 1 AND f.i5 >= 0 THEN f.i5 END)), SUM(f.text LIKE 'guard_%')"
       " FROM events r LEFT JOIN events f ON f.type='fill' AND f.sig=r.sig"
       " WHERE r.type='route' AND r.i5=0 AND r.ts >= ?1 GROUP BY r.text ORDER BY 2 DESC");
  rows("\nCU price / limit by leader class (buys: avg cu_price / avg cu_limit / jito / routed):",
       "SELECT text, printf('%.0f', AVG(i1)), printf('%.0f', AVG(i2)), SUM(i3), SUM(i7) FROM events"
       " WHERE type='route' AND i5=0 AND ts >= ?1 GROUP BY text");
  rows("\nGuard reverts (reason / n / SOL burnt in fees):",
       "SELECT text, COUNT(*), printf('%.6f', -SUM(i0) / 1e9) FROM events WHERE type='position_close'"
       " AND text LIKE 'beaten_%' AND ts >= ?1 GROUP BY text");
  rows("\nBeaten by (rival guard reverts per wallet):",
       "SELECT c, COUNT(*) FROM events WHERE type='fill' AND text='guard_rival' AND ts >= ?1 GROUP BY c ORDER BY 2 DESC LIMIT 10");
  rows("\nRivals ahead of our buys (wallet / pools / txs / max cu_price):",
       (std::string("SELECT a.signer, COUNT(DISTINCT a.pool), COUNT(*), MAX(a.cu_price) FROM (") +
        "SELECT r.a AS signer, r.b AS pool, r.i1 AS cu_price FROM events r JOIN events o ON o.type='order' AND o.i0=0 AND o.b=r.b"
        " LEFT JOIN events f ON f.type='fill' AND f.i0=0 AND f.sig=o.sig AND f.d5=1 AND f.i6>=0"
        " WHERE r.type='rival_tx' AND r.i4>0 AND r.d1<0.5 AND r.ts >= ?1"
        " AND (f.id IS NULL OR r.slot < f.slot OR (r.slot = f.slot AND r.i0 >= 0 AND f.i5 >= 0 AND r.i0 < f.i5))"
        ") a GROUP BY a.signer ORDER BY 2 DESC, 3 DESC LIMIT 10").c_str());
  rows("\nPool txs in the window by leader class and route (class / route / n / avg cu_price):",
       "SELECT CASE i5 WHEN 1 THEN 'bam' WHEN 2 THEN 'firedancer' ELSE 'agave' END,"
       " CASE CAST(d0 AS INTEGER) WHEN 1 THEN 'sender' WHEN 2 THEN 'jito' WHEN 3 THEN 'other' ELSE 'none' END,"
       " COUNT(*), printf('%.0f', AVG(i1)) FROM events WHERE type='rival_tx' AND ts >= ?1 GROUP BY 1, 2 ORDER BY 1, 3 DESC");
  rows("\nSender 429s (n):", "SELECT COUNT(*) FROM events WHERE type='send_ack' AND i1=429 AND ts >= ?1");
  rows("\nSend errors (route / error / n):",
       "SELECT route, substr(error, 1, 60), COUNT(*) FROM acks WHERE ok=0 AND ts >= ?1 GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 10");
  sqlite3_close(db);
  return 0;
}
