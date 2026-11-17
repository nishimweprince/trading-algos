#pragma once
// Feed health: is the LaserStream picture current enough to trade on? Nov 11-16 2026 every shard reconnected
// every 10 s ("subscribed" -> "read failed") for 4.3 days and nothing noticed: `up` flickered true, the update
// age gauge was never read, and the builder passed a feed age of 0 to the limits. This turns the raw signals
// (shards up, reconnect rate, newest update age) into one gate for sending plus alert transitions.
#include <cstdint>
#include <deque>
#include <string>
#include <utility>

namespace arb::feed {

struct FeedHealthConfig {
  int64_t max_update_age_ms = 800;   // risk.max_feed_lag_ms
  int flap_reconnects_per_min = 3;   // reconnects across all shards in 60 s that count as flapping
  int64_t alert_after_ms = 60'000;   // unhealthy this long before the first alert
  int64_t recover_after_ms = 60'000; // healthy this long before "recovered"
  int64_t repeat_ms = 1'800'000;     // reminder while still down
};

struct FeedHealthSample {
  int64_t now_ms = 0;
  int shards_up = 0, shards_total = 0;
  uint64_t reconnects_total = 0;     // cumulative across shards
  int64_t update_age_ms = -1;        // newest account update; -1 = none yet
};

enum class FeedEvent : uint8_t { none, down, still_down, recovered };

class FeedHealth {
 public:
  explicit FeedHealth(FeedHealthConfig c = {}) : c_(c) {}
  /** Feed one sample (about once a second); returns an alert transition, if any. */
  FeedEvent update(const FeedHealthSample& s);
  /** Safe to send right now (no grace period: the gate closes on the first bad sample). */
  bool ok() const { return ok_; }
  /** Why the gate is closed ("" when ok). */
  const std::string& reason() const { return reason_; }
  int reconnects_last_min() const { return per_min_; }
  int64_t down_since_ms() const { return bad_since_; }

 private:
  FeedHealthConfig c_;
  std::deque<std::pair<int64_t, uint64_t>> hist_;  // (ms, reconnects_total), last 60 s
  bool ok_ = false, alerted_ = false;
  int per_min_ = 0;
  int64_t bad_since_ = -1, good_since_ = -1, last_alert_ = 0;
  std::string reason_ = "no sample yet";
};

}  // namespace arb::feed
