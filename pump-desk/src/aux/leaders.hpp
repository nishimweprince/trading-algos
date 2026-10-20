#pragma once
// The leader schedule, classified by how each leader orders transactions:
//   Agave / Jito without BAM — priority (CU price) decides
//   BAM                       — arrival at the BAM node decides; priority doesn't count
//   Firedancer / Frankendancer — fee per requested CU decides
// aux-leaders refreshes it off the hot path and publishes an immutable table
// (current + next epoch) through Shared::leaders; logic does one array read.
#include <memory>
#include <string>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#include "core/config.hpp"
#include "logic/shared.hpp"

namespace pd {

class Logger;

/** getLeaderSchedule result: identity -> slot indexes within its epoch. */
using Schedule = std::vector<std::pair<std::string, std::vector<uint32_t>>>;

Schedule parse_leader_schedule(std::string_view body);
/** getClusterNodes result: identity -> version ("" when the node reports none). */
std::unordered_map<std::string, std::string> parse_cluster_versions(std::string_view body);
/** A BAM validator list: ["identity", ...], [{"identity": ...}], or {"validators": [...]}. Throws on bad JSON. */
std::unordered_set<std::string> parse_bam_list(std::string_view json);

/** Firedancer and Frankendancer report 0.x versions; Agave and Jito-Solana report 1.x and up. */
LeaderClass classify_version(std::string_view version);
/** An identity on the BAM list is Bam, whatever its version. */
LeaderClass classify_leader(const std::string& identity, const std::unordered_map<std::string, std::string>& versions,
                            const std::unordered_set<std::string>& bam);

/** Lay `epochs` (consecutive, slots_per_epoch each, the first starting at first_slot) into one table. */
std::unique_ptr<LeaderTable> build_leader_table(uint64_t first_slot, uint64_t slots_per_epoch, const std::vector<Schedule>& epochs,
                                                const std::unordered_map<std::string, std::string>& versions,
                                                const std::unordered_set<std::string>& bam);

void run_aux_leaders(const Config& cfg, Shared& sh, Logger& log);

}  // namespace pd
