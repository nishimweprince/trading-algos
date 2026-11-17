#pragma once
// Jito's tip floor (landed-tip percentiles), polled on its own thread so the live loop never waits on it.
// Until Nov 17 2026 the parser existed but nothing called it: tip_min was always the 1,000-lamport config floor.
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>

namespace arb::submit {

class TipFloorPoller {
 public:
  /** pct: which landed-tip percentile to report (25 per spec §6.6); refresh_ms: poll period. */
  TipFloorPoller(std::string url, int pct, int refresh_ms);
  ~TipFloorPoller();
  TipFloorPoller(const TipFloorPoller&) = delete;
  TipFloorPoller& operator=(const TipFloorPoller&) = delete;
  /** Lamports, or 0 when the last good read is older than max_age_ms (or there was none). */
  uint64_t value(int64_t now_ms, int64_t max_age_ms) const;
  uint64_t polls() const { return polls_.load(); }
  uint64_t failures() const { return failures_.load(); }

 private:
  void run();
  std::string url_;
  int pct_, refresh_ms_;
  std::atomic<uint64_t> lamports_{0}, polls_{0}, failures_{0};
  std::atomic<int64_t> ok_ms_{0};
  std::mutex mu_;
  std::condition_variable cv_;
  bool stop_ = false;
  std::thread th_;
};

}  // namespace arb::submit
