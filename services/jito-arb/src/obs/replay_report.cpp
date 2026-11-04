#include "obs/replay_report.hpp"

#include <algorithm>
#include <cstdio>
#include <ctime>

#include "engine/costs.hpp"

namespace arb::obs {

namespace {

std::string day_of(int64_t wall_ms) {
  time_t t = time_t(wall_ms / 1000);
  tm g;
  gmtime_r(&t, &g);
  char buf[16];
  std::strftime(buf, sizeof buf, "%Y-%m-%d", &g);
  return buf;
}

bool priced_ok(const DecisionRecord& r) {
  if (r.action != uint8_t(Action::Skipped)) return true;
  auto why = engine::Reason(r.reason);
  return why == engine::Reason::pool_conflict || why == engine::Reason::slot_cap || why == engine::Reason::send_lag;
}

uint64_t median(std::vector<uint64_t> v) {
  if (v.empty()) return 0;
  std::nth_element(v.begin(), v.begin() + long(v.size() / 2), v.end());
  return v[v.size() / 2];
}

}  // namespace

ReplayReport aggregate(const std::vector<DecisionRecord>& recs, int64_t min_ev, uint64_t min_net) {
  ReplayReport out;
  for (const DecisionRecord& r : recs) {
    if (r.kind != uint8_t(RecordKind::Candidate)) continue;
    ++out.records;
    DayStats& d = out.days[day_of(r.wall_ms)];
    ++d.candidates;
    if (!priced_ok(r)) {
      ++d.reasons[r.reason];
      continue;
    }
    if (r.ev_lamports < min_ev) continue;
    ++d.detections;
    if (r.action != uint8_t(Action::Skipped)) ++d.dispatched;
    else ++d.reasons[r.reason];
    if (r.net_lamports >= min_ev) ++d.gate_passes;
    ++d.by_route_class[r.route_class];
    d.x.push_back(r.x);
    d.sum_gross += r.gross_lamports;
    d.sum_tip += r.tip_lamports;
    uint64_t reserved = r.fee_lamports + min_net;
    d.sum_tip_ceiling += r.gross_lamports > reserved ? r.gross_lamports - reserved : 0;
    d.sum_net += r.net_lamports;
    d.sum_ev += r.ev_lamports;
  }
  return out;
}

std::string to_text(const ReplayReport& r) {
  std::string s;
  char line[512];
  std::snprintf(line, sizeof line, "%-10s %10s %10s %10s %10s %14s %14s %14s %14s %14s %14s\n", "day", "candidates",
                "detections", "dispatched", "gate", "median_x", "gross_lam", "tip_lam", "tip_ceil_lam", "net_lam", "ev_lam");
  s += line;
  for (auto& [day, d] : r.days) {
    std::snprintf(line, sizeof line, "%-10s %10llu %10llu %10llu %10llu %14llu %14llu %14llu %14llu %14lld %14lld\n", day.c_str(),
                  (unsigned long long)d.candidates, (unsigned long long)d.detections, (unsigned long long)d.dispatched,
                  (unsigned long long)d.gate_passes, (unsigned long long)median(d.x), (unsigned long long)d.sum_gross,
                  (unsigned long long)d.sum_tip, (unsigned long long)d.sum_tip_ceiling, (long long)d.sum_net, (long long)d.sum_ev);
    s += line;
    for (auto& [why, n] : d.reasons) {
      std::snprintf(line, sizeof line, "    skipped %-20s %llu\n", engine::reason_name(engine::Reason(why)), (unsigned long long)n);
      s += line;
    }
  }
  std::snprintf(line, sizeof line, "records %llu\n", (unsigned long long)r.records);
  s += line;
  return s;
}

std::string to_json(const ReplayReport& r) {
  std::string s = "{\"records\":" + std::to_string(r.records) + ",\"days\":{";
  bool first = true;
  for (auto& [day, d] : r.days) {
    if (!first) s += ",";
    first = false;
    s += "\"" + day + "\":{\"candidates\":" + std::to_string(d.candidates) + ",\"detections\":" + std::to_string(d.detections) +
         ",\"dispatched\":" + std::to_string(d.dispatched) + ",\"gate_passes\":" + std::to_string(d.gate_passes) +
         ",\"median_x\":" + std::to_string(median(d.x)) + ",\"gross_lamports\":" + std::to_string(d.sum_gross) +
         ",\"tip_lamports\":" + std::to_string(d.sum_tip) + ",\"tip_ceiling_lamports\":" + std::to_string(d.sum_tip_ceiling) +
         ",\"net_lamports\":" + std::to_string(d.sum_net) + ",\"ev_lamports\":" + std::to_string(d.sum_ev) + ",\"skipped\":{";
    bool f2 = true;
    for (auto& [why, n] : d.reasons) {
      if (!f2) s += ",";
      f2 = false;
      s += "\"" + std::string(engine::reason_name(engine::Reason(why))) + "\":" + std::to_string(n);
    }
    s += "}}";
  }
  return s + "}}";
}

std::vector<DecisionRecord> read_records(const std::vector<std::string>& paths) {
  std::vector<DecisionRecord> out;
  for (const std::string& p : paths) {
    DecisionReader rd;
    if (!rd.open(p)) continue;
    DecisionRecord r;
    while (rd.next(r)) out.push_back(r);
  }
  return out;
}

}  // namespace arb::obs
