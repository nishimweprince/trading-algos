#pragma once
// Leader map (18 Nov 2026): who leads each slot, whether they run Jito and BAM, and which Jito region is nearest.
// 105 sent bundles went to 61% block-engine Jito leaders, 37% BAM, 2% non-Jito; routing to the leader's nearest
// region and skipping slots no Jito path can reach needs this per slot. A background thread refreshes it every
// 10 minutes (getLeaderSchedule for this epoch and the next, Jito's Kobe validator list, getClusterNodes gossip
// IPs, and a batch geolocation of those IPs). Reads never block: the map is swapped in whole.
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

namespace arb::submit {

enum class LeaderKind : uint8_t { unknown = 0, jito, bam, not_jito };
const char* leader_kind_name(LeaderKind k);

struct LeaderInfo {
  std::string identity;   // base58; empty when the slot is not in the map
  LeaderKind kind = LeaderKind::unknown;
  std::string region;     // nearest configured Jito region ("" when the location is unknown)
  std::string place;      // country and city, for the log
};

/** Jito block-engine regions and their coordinates (lat, lon). */
struct RegionPoint {
  const char* name;
  double lat, lon;
};
const std::vector<RegionPoint>& jito_regions();
/** Nearest of `allowed` to (lat, lon) by great-circle distance; "" when none is allowed. */
std::string nearest_region(double lat, double lon, const std::vector<std::string>& allowed);

class LeaderMap {
 public:
  /** `rpc_url`: Helius RPC; `regions`: the regions routing may choose from. Starts its refresh thread. */
  LeaderMap(std::string rpc_url, std::vector<std::string> regions, int refresh_s = 600);
  ~LeaderMap();
  LeaderMap(const LeaderMap&) = delete;
  LeaderMap& operator=(const LeaderMap&) = delete;
  LeaderInfo at(uint64_t slot) const;
  bool ready() const { return ready_.load(); }
  /** Refresh now (the thread's body); public for tests with a fake source. */
  void refresh();

 private:
  struct Validator {
    LeaderKind kind = LeaderKind::not_jito;
    std::string region, place;
  };
  struct Snapshot {
    std::vector<std::pair<uint64_t, std::vector<uint32_t>>> epochs;  // (first slot, validator index per slot)
    std::vector<std::string> identities;
    std::vector<Validator> validators;
  };
  void run();
  std::string url_;
  std::vector<std::string> regions_;
  int refresh_s_;
  mutable std::mutex mu_;
  std::shared_ptr<const Snapshot> snap_;
  std::map<std::string, std::pair<std::string, std::pair<double, double>>> geo_;  // ip -> (place, lat/lon), cached
  std::atomic<bool> ready_{false}, stop_{false};
  std::mutex cv_mu_;
  std::condition_variable cv_;
  std::thread th_;
};

}  // namespace arb::submit
