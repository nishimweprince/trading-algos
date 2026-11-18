#pragma once
// Plain-transaction (Helius Sender) path, 19 Nov 2026. On 18 Nov our bundle reached the Frankfurt block engine
// before the winning transaction in 62 of 62 measured losses (median 40 ms early), and 55 of those winners were
// plain priority-fee transactions with no Jito tip. So each arb can also go out as a plain transaction. Unlike a
// bundle, a plain transaction that fails (the edge is gone: MinOutNotMet) still pays its base and priority fees.
// This file holds the two pure pieces that bound that cost:
//   sender_fee():  what to bid, as a fraction of the opportunity's gross;
//   SenderBudget:  a daily cap on fees burnt by failed sends, plus a pause after a run of failures.
#include <cstdint>
#include <map>
#include <mutex>
#include <string>

namespace arb::risk {

struct SenderFeeConfig {
  uint32_t fee_frac_ppm = 100'000;            // total fee (base + priority) at most this share of gross
  uint64_t max_cu_price = 5'000'000;          // micro-lamports per CU, hard ceiling
  uint64_t min_cu_price = 1'000;              // below this the bid is pointless: skip the Sender path
  uint64_t base_fee_lamports = 5'000;         // one signature
  uint64_t tip_lamports = 10'000;             // Sender tip (paid only when the transaction succeeds)
};

struct SenderFee {
  bool ok = false;
  uint64_t cu_price = 0;          // micro-lamports per CU
  uint64_t fee_lamports = 0;      // base + priority: what a failed send costs
  const char* why = "";
};

/**
 * Bid for one opportunity: the priority fee fills fee_frac of gross minus the base fee and the tip, capped at
 * max_cu_price. Priority fee lamports = ceil(cu_price * cu_limit / 1e6). Not ok when the opportunity cannot
 * pay at least min_cu_price.
 */
SenderFee sender_fee(uint64_t gross_lamports, uint32_t cu_limit, const SenderFeeConfig& c);

struct SenderBudgetConfig {
  uint64_t daily_fail_cap_lamports = 50'000'000;  // fees burnt by failed sends per UTC day (0.05 SOL)
  int max_consecutive_fail = 50;                   // then pause
  int pause_s = 600;
};

enum class SenderBlock : uint8_t { none = 0, disabled, daily_cap, paused };
const char* sender_block_name(SenderBlock b);

/**
 * Counts every send's fee as at risk until its outcome is known: failed (the fee was paid, counts against the
 * cap), landed (paid, but it earned), or dropped (never landed: nothing paid). A send is allowed only while
 * failed_today + at_risk + this fee stays within the cap, so the cap holds even before outcomes arrive.
 * Thread-safe: the engine thread checks and records sends, the Sender status thread records outcomes.
 */
class SenderBudget {
 public:
  explicit SenderBudget(SenderBudgetConfig c = {}) : c_(c) {}
  SenderBlock check(int64_t wall_ms, uint64_t fee_lamports);
  void on_send(const std::string& sig, uint64_t fee_lamports, int64_t wall_ms);
  enum class Result : uint8_t { landed = 1, failed = 2, dropped = 3 };
  /** Returns the fee recorded for the signature (0 when unknown). */
  uint64_t on_result(const std::string& sig, Result r, int64_t wall_ms);
  uint64_t failed_today(int64_t wall_ms);
  uint64_t at_risk() const {
    std::lock_guard<std::mutex> g(mu_);
    return at_risk_;
  }
  size_t pending() const {
    std::lock_guard<std::mutex> g(mu_);
    return open_.size();
  }
  int consecutive_failures() const {
    std::lock_guard<std::mutex> g(mu_);
    return consecutive_fail_;
  }
  bool paused(int64_t wall_ms) const {
    std::lock_guard<std::mutex> g(mu_);
    return wall_ms < paused_until_ms_;
  }

 private:
  void roll(int64_t wall_ms);
  mutable std::mutex mu_;
  SenderBudgetConfig c_;
  int64_t day_ = -1;
  uint64_t failed_today_ = 0, at_risk_ = 0;
  int consecutive_fail_ = 0;
  int64_t paused_until_ms_ = 0;
  std::map<std::string, uint64_t> open_;  // signature -> fee at risk
};

}  // namespace arb::risk
