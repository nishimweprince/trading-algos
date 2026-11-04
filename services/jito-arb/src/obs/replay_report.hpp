#pragma once
// Replay report (spec §13 phase 2): per UTC day, the detections in a decision log with size, gross
// profit, the policy tip, the tip ceiling (the most the bot could bid and still clear min_net, i.e. the
// tip it would need to beat a rival bidding that much) and net EV. The gate after phase 2 counts days'
// detections whose net EV clears min_ev after a realistic tip.
#include <cstdint>
#include <map>
#include <string>
#include <vector>

#include "obs/decision_log.hpp"

namespace arb::obs {

struct DayStats {
  uint64_t candidates = 0;  // sized candidates (every record)
  uint64_t detections = 0;  // priced above min_ev (sent, dry-run, or dropped only by dispatch rules)
  uint64_t dispatched = 0;  // sent or dry-run
  uint64_t gate_passes = 0; // detections whose net after the policy tip clears min_ev (EV if it lands)
  uint64_t sum_gross = 0, sum_tip = 0, sum_tip_ceiling = 0;
  int64_t sum_net = 0, sum_ev = 0;
  std::vector<uint64_t> x;  // base units, for the median
  std::map<uint8_t, uint64_t> reasons;
  std::map<uint16_t, uint64_t> by_route_class;
};

struct ReplayReport {
  std::map<std::string, DayStats> days;  // "YYYY-MM-DD" UTC
  uint64_t records = 0;
};

/**
 * Aggregate records. `min_ev_lamports` and `min_net_lamports` match the engine's. The tip in a record
 * is the policy tip (alpha share of gross, at least Jito's p25 floor when live): the "realistic tip"
 * of the phase-2 gate. The tip ceiling is what a rival would have to outbid.
 */
ReplayReport aggregate(const std::vector<DecisionRecord>& recs, int64_t min_ev_lamports, uint64_t min_net_lamports);
std::string to_text(const ReplayReport& r);
std::string to_json(const ReplayReport& r);

/** Read every record of the given decision-log files. */
std::vector<DecisionRecord> read_records(const std::vector<std::string>& paths);

}  // namespace arb::obs
