#pragma once
// Limits and kill switches (spec §10 table) and the 24-hour denylist. One object, consulted by the
// submitter before every send and fed by outcomes; it never sends anything itself.
#include <cstdint>
#include <map>
#include <string>
#include <vector>

#include "core/config.hpp"
#include "core/types.hpp"

namespace arb::risk {

enum class Block : uint8_t {
  none = 0, kill_switch, daily_fee_cap, sol_float_low, reject_pause, feed_lag, dex_disabled,
};
const char* block_name(Block b);

struct LimitsConfig {
  uint64_t max_daily_fee_spend_lamports = 200'000'000;
  uint64_t min_sol_float_lamports = 300'000'000;
  uint64_t max_sol_float_lamports = 2'000'000'000;
  int max_consecutive_rejects = 200;
  int reject_pause_s = 300;
  int max_feed_lag_ms = 800;
  uint32_t quote_mismatch_ppm = 20'000;  // 2% of simulated sends
  int quote_mismatch_min_samples = 50;
};
LimitsConfig limits_config(const Config& c);

class Limits {
 public:
  explicit Limits(LimitsConfig c = {}) : c_(c) {}
  /** May a bundle be sent now? `wall_ms` is UTC wall time; `feed_age_ms` the newest update's age. */
  Block check(int64_t wall_ms, uint64_t wallet_lamports, int64_t feed_age_ms, uint8_t dex_mask) const;

  void set_kill(bool on) { kill_ = on; }  // KILL file or Telegram /stop
  bool killed() const { return kill_; }
  /** Base + priority + Sender fees actually paid (landed or failed on the Sender path). */
  void record_fee(int64_t wall_ms, uint64_t lamports);
  uint64_t fees_today(int64_t wall_ms) const;
  /** Jito rejected the bundle (error) or accepted it; a run of rejects pauses sending. */
  void record_submit(int64_t wall_ms, bool rejected);
  /** Pre-send simulation vs quote, per DEX. Above the mismatch rate (after min samples) the DEX is disabled. */
  void record_sim(uint8_t dex, bool mismatch);
  bool dex_disabled(uint8_t dex) const;
  /** Excess over max_sol_float, for the sweep job (0 if none). */
  uint64_t sweep_excess(uint64_t wallet_lamports) const;

 private:
  LimitsConfig c_;
  bool kill_ = false;
  int64_t fee_day_ = -1;
  uint64_t fee_spent_ = 0;
  int consecutive_rejects_ = 0;
  int64_t paused_until_ms_ = 0;
  struct SimStats {
    uint64_t n = 0, mismatches = 0;
    bool disabled = false;
  };
  std::map<uint8_t, SimStats> sims_;
};

/** denylist.json: {"mints":[..],"pools":[..]} permanent, plus entries added at runtime that expire after ttl. */
class Denylist {
 public:
  explicit Denylist(int ttl_s = 86400) : ttl_ms_(int64_t(ttl_s) * 1000) {}
  bool load(const std::string& body, std::string* err = nullptr);
  /** Add for ttl (a reverted simulation with an unexpected token error). */
  void add(const Pubkey& k, int64_t wall_ms) { expiring_[k] = wall_ms + ttl_ms_; }
  bool contains(const Pubkey& k, int64_t wall_ms) const;
  size_t size() const { return permanent_.size() + expiring_.size(); }

 private:
  int64_t ttl_ms_;
  std::vector<Pubkey> permanent_;
  std::map<Pubkey, int64_t> expiring_;
};

}  // namespace arb::risk
