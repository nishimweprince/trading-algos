// pump-desk-report: net PnL, landing and latency from the run database.
//
//   pump-desk-report [data/pump-desk.db] [--since-hours N]
#include <sqlite3.h>

#include <algorithm>
#include <cstdio>
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

}  // namespace

int main(int argc, char** argv) {
  std::string path = "data/pump-desk.db";
  double hours = 0;
  for (int i = 1; i < argc; ++i) {
    if (!std::strcmp(argv[i], "--since-hours") && i + 1 < argc) hours = std::atof(argv[++i]);
    else path = argv[i];
  }
  if (sqlite3_open_v2(path.c_str(), &db, SQLITE_OPEN_READONLY, nullptr) != SQLITE_OK) {
    std::fprintf(stderr, "cannot open %s\n", path.c_str());
    return 1;
  }
  if (hours > 0) since_ms = (long long)(std::time(nullptr)) * 1000 - (long long)(hours * 3600 * 1000);
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
  rows("\nSender 429s (n):", "SELECT COUNT(*) FROM events WHERE type='send_ack' AND i1=429 AND ts >= ?1");
  rows("\nSend errors (route / error / n):",
       "SELECT route, substr(error, 1, 60), COUNT(*) FROM acks WHERE ok=0 AND ts >= ?1 GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 10");
  sqlite3_close(db);
  return 0;
}
