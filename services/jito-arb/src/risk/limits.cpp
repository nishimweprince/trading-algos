#include "risk/limits.hpp"

#include <simdjson.h>

#include <algorithm>
#include <cmath>

#include "core/base58.hpp"

namespace arb::risk {

const char* block_name(Block b) {
  switch (b) {
    case Block::none: return "none";
    case Block::kill_switch: return "kill_switch";
    case Block::daily_fee_cap: return "daily_fee_cap";
    case Block::sol_float_low: return "sol_float_low";
    case Block::reject_pause: return "reject_pause";
    case Block::feed_lag: return "feed_lag";
    case Block::dex_disabled: return "dex_disabled";
  }
  return "unknown";
}

LimitsConfig limits_config(const Config& c) {
  auto lam = [](double sol) { return uint64_t(std::llround(sol * double(LAMPORTS_PER_SOL))); };
  LimitsConfig l;
  l.max_daily_fee_spend_lamports = lam(c.risk.max_daily_fee_spend_sol);
  l.min_sol_float_lamports = lam(c.risk.min_sol_float);
  l.max_sol_float_lamports = lam(c.risk.max_sol_float);
  l.max_consecutive_rejects = c.risk.max_consecutive_rejects;
  l.reject_pause_s = c.risk.reject_pause_s;
  l.max_feed_lag_ms = c.risk.max_feed_lag_ms;
  l.quote_mismatch_ppm = uint32_t(std::llround(c.risk.quote_mismatch_rate * 1e6));
  l.quote_mismatch_min_samples = c.risk.quote_mismatch_min_samples;
  return l;
}

namespace {
int64_t utc_day(int64_t wall_ms) { return wall_ms / 86'400'000LL; }
}  // namespace

Block Limits::check(int64_t wall_ms, uint64_t wallet_lamports, int64_t feed_age_ms, uint8_t dex_mask) const {
  if (kill_) return Block::kill_switch;
  if (fees_today(wall_ms) >= c_.max_daily_fee_spend_lamports) return Block::daily_fee_cap;
  if (wallet_lamports < c_.min_sol_float_lamports) return Block::sol_float_low;
  if (wall_ms < paused_until_ms_) return Block::reject_pause;
  if (feed_age_ms > c_.max_feed_lag_ms) return Block::feed_lag;
  for (uint8_t d = 0; d < 8; ++d)
    if ((dex_mask >> d) & 1 && dex_disabled(d)) return Block::dex_disabled;
  return Block::none;
}

void Limits::record_fee(int64_t wall_ms, uint64_t lamports) {
  if (utc_day(wall_ms) != fee_day_) {
    fee_day_ = utc_day(wall_ms);
    fee_spent_ = 0;
  }
  fee_spent_ += lamports;
}

uint64_t Limits::fees_today(int64_t wall_ms) const { return utc_day(wall_ms) == fee_day_ ? fee_spent_ : 0; }

void Limits::record_submit(int64_t wall_ms, bool rejected) {
  if (!rejected) {
    consecutive_rejects_ = 0;
    return;
  }
  if (++consecutive_rejects_ >= c_.max_consecutive_rejects) {
    paused_until_ms_ = wall_ms + int64_t(c_.reject_pause_s) * 1000;
    consecutive_rejects_ = 0;
  }
}

void Limits::record_sim(uint8_t dex, bool mismatch) {
  SimStats& s = sims_[dex];
  ++s.n;
  if (mismatch) ++s.mismatches;
  if (s.n >= uint64_t(c_.quote_mismatch_min_samples) && s.mismatches * 1'000'000 > s.n * c_.quote_mismatch_ppm) s.disabled = true;
}

bool Limits::dex_disabled(uint8_t dex) const {
  auto it = sims_.find(dex);
  return it != sims_.end() && it->second.disabled;
}

uint64_t Limits::sweep_excess(uint64_t wallet_lamports) const {
  return wallet_lamports > c_.max_sol_float_lamports ? wallet_lamports - c_.max_sol_float_lamports : 0;
}

bool Denylist::load(const std::string& body, std::string* err) {
  simdjson::dom::parser p;
  simdjson::padded_string ps(body);
  simdjson::dom::element root;
  if (p.parse(ps).get(root) != simdjson::SUCCESS) {
    if (err) *err = "denylist: bad JSON";
    return false;
  }
  for (const char* field : {"mints", "pools"}) {
    simdjson::dom::array a;
    if (root[field].get(a) != simdjson::SUCCESS) continue;
    for (auto e : a) {
      std::string_view s;
      Pubkey k;
      if (e.get(s) != simdjson::SUCCESS || !b58_decode32(s, k)) {
        if (err) *err = std::string("denylist: bad key in ") + field;
        return false;
      }
      permanent_.push_back(k);
    }
  }
  return true;
}

bool Denylist::contains(const Pubkey& k, int64_t wall_ms) const {
  if (std::find(permanent_.begin(), permanent_.end(), k) != permanent_.end()) return true;
  auto it = expiring_.find(k);
  return it != expiring_.end() && wall_ms < it->second;
}

}  // namespace arb::risk
