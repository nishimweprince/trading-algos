#include "risk/sender_budget.hpp"

#include <algorithm>

namespace arb::risk {

SenderFee sender_fee(uint64_t gross, uint32_t cu_limit, const SenderFeeConfig& c) {
  SenderFee f;
  if (cu_limit == 0) {
    f.why = "no compute limit";
    return f;
  }
  // u128: gross * ppm and price * limit both stay far below 2^64 today, but this is cheap insurance.
  unsigned __int128 budget = (unsigned __int128)gross * c.fee_frac_ppm / 1'000'000;
  uint64_t fixed = c.base_fee_lamports + c.tip_lamports;
  if (c.max_tip_share_ppm) {
    if ((unsigned __int128)c.tip_lamports * 1'000'000 > (unsigned __int128)gross * c.max_tip_share_ppm) {
      f.why = "gross too small for the tip";
      return f;
    }
    fixed = c.base_fee_lamports;
  }
  if (budget <= fixed) {
    f.why = "gross too small for the fee share";
    return f;
  }
  unsigned __int128 priority = budget - fixed;
  unsigned __int128 price = priority * 1'000'000 / cu_limit;
  if (price > c.max_cu_price) price = c.max_cu_price;
  if (price < c.min_cu_price) {
    f.why = "priority fee below the minimum price";
    return f;
  }
  f.ok = true;
  f.cu_price = uint64_t(price);
  unsigned __int128 pri_lamports = (price * cu_limit + 999'999) / 1'000'000;  // the runtime rounds up
  f.fee_lamports = c.base_fee_lamports + uint64_t(pri_lamports);
  return f;
}

const char* sender_block_name(SenderBlock b) {
  switch (b) {
    case SenderBlock::none: return "none";
    case SenderBlock::disabled: return "disabled";
    case SenderBlock::daily_cap: return "daily_cap";
    case SenderBlock::paused: return "paused";
  }
  return "?";
}

void SenderBudget::roll(int64_t wall_ms) {
  int64_t day = wall_ms / 86'400'000;
  if (day != day_) {
    day_ = day;
    failed_today_ = 0;
  }
}

SenderBlock SenderBudget::check(int64_t wall_ms, uint64_t fee) {
  std::lock_guard<std::mutex> g(mu_);
  roll(wall_ms);
  if (wall_ms < paused_until_ms_) return SenderBlock::paused;
  if (failed_today_ + at_risk_ + fee > c_.daily_fail_cap_lamports) return SenderBlock::daily_cap;
  return SenderBlock::none;
}

void SenderBudget::on_send(const std::string& sig, uint64_t fee, int64_t wall_ms) {
  std::lock_guard<std::mutex> g(mu_);
  roll(wall_ms);
  auto [it, fresh] = open_.emplace(sig, fee);
  if (fresh) at_risk_ += fee;
}

uint64_t SenderBudget::on_result(const std::string& sig, Result r, int64_t wall_ms) {
  std::lock_guard<std::mutex> g(mu_);
  roll(wall_ms);
  auto it = open_.find(sig);
  if (it == open_.end()) return 0;
  uint64_t fee = it->second;
  at_risk_ -= std::min(at_risk_, fee);
  open_.erase(it);
  switch (r) {
    case Result::failed:
      failed_today_ += fee;
      if (++consecutive_fail_ >= c_.max_consecutive_fail) {
        paused_until_ms_ = wall_ms + int64_t(c_.pause_s) * 1000;
        consecutive_fail_ = 0;
      }
      break;
    case Result::landed:
      consecutive_fail_ = 0;
      break;
    case Result::dropped:
      break;  // never landed: nothing paid, and no evidence either way
  }
  return fee;
}

uint64_t SenderBudget::failed_today(int64_t wall_ms) {
  std::lock_guard<std::mutex> g(mu_);
  roll(wall_ms);
  return failed_today_;
}

}  // namespace arb::risk
