#include "engine/costs.hpp"

#include <algorithm>

#include "core/u256.hpp"

namespace arb::engine {

const char* reason_name(Reason r) {
  static const char* names[] = {"none", "base_price_unknown", "no_profit", "cu_over_budget", "below_min_net",
                                "guard_overflow", "guard_above_quote", "below_min_ev", "pool_conflict", "slot_cap",
                                "send_lag", "sizer_failed", "boundary", "flash_disabled", "risk_block",
                                "tx_build_failed", "sign_guard"};
  size_t i = size_t(r);
  return i < sizeof names / sizeof *names ? names[i] : "unknown";
}

uint64_t BasePrice::to_lamports(uint64_t units) const {
  U256 p = U256::mul(units, lamports_per_unit_x64).shr(64);
  return p.fits_u128() && p.lo128() <= UINT64_MAX ? uint64_t(p.lo128()) : UINT64_MAX;
}

uint64_t BasePrice::from_lamports_ceil(uint64_t lamports) const {
  if (lamports_per_unit_x64 == 0) return UINT64_MAX;
  U256 num = U256::mul(lamports, u128(1) << 64), q, r;
  num.divmod(U256::from_u128(lamports_per_unit_x64), q, r);
  if (!q.fits_u128() || q.lo128() >= UINT64_MAX) return UINT64_MAX;
  return uint64_t(q.lo128()) + (r.is_zero() ? 0 : 1);
}

TipPolicy::Class& TipPolicy::cls(uint16_t rc) {
  auto [it, inserted] = classes_.try_emplace(rc);
  if (inserted) it->second.alpha_ppm = p_.alpha_init_ppm;
  return it->second;
}

uint32_t TipPolicy::alpha_ppm(uint16_t rc) const {
  auto it = classes_.find(rc);
  return it == classes_.end() ? p_.alpha_init_ppm : it->second.alpha_ppm;
}

uint32_t TipPolicy::p_land_ppm(uint16_t rc) const {
  auto it = classes_.find(rc);
  if (it == classes_.end()) return p_.p_land_init_ppm;
  // (landed + prior) / (attempts + prior_n) with the init rate worth 10 pseudo-attempts.
  constexpr uint64_t PRIOR_N = 10;
  uint64_t num = it->second.landed * 1'000'000 + uint64_t(p_.p_land_init_ppm) * PRIOR_N;
  return uint32_t(num / (it->second.attempts + PRIOR_N));
}

void TipPolicy::record(uint16_t rc, bool landed) {
  Class& c = cls(rc);
  ++c.attempts;
  ++c.window_attempts;
  if (landed) {
    ++c.landed;
    ++c.window_landed;
  }
  if (c.window_attempts < p_.window) return;
  uint64_t rate = uint64_t(c.window_landed) * 1'000'000 / c.window_attempts;
  if (rate < p_.target_land_ppm) c.alpha_ppm = std::min(p_.alpha_max_ppm, c.alpha_ppm + p_.step_ppm);
  else if (rate > p_.target_land_ppm) c.alpha_ppm = c.alpha_ppm > p_.step_ppm ? std::max(p_.alpha_min_ppm, c.alpha_ppm - p_.step_ppm) : p_.alpha_min_ppm;
  c.window_attempts = c.window_landed = 0;
}

Pricing price(uint64_t x, int64_t profit_base, uint64_t flash_fee, uint32_t route_cu, uint16_t route_class,
              const BasePrice& bp, const TipPolicy& tips, const CostConfig& cfg) {
  Pricing pr;
  if (!bp.known) { pr.reject = Reason::base_price_unknown; return pr; }
  if (profit_base <= 0) { pr.reject = Reason::no_profit; return pr; }
  uint64_t cu = route_cu + cfg.cu_overhead;
  cu += uint32_t(uint64_t(cu) * cfg.cu_headroom_ppm / 1'000'000);
  if (cu > cfg.max_route_cu) { pr.reject = Reason::cu_over_budget; return pr; }
  pr.cu_limit = uint32_t(cu);
  pr.gross_lamports = bp.to_lamports(uint64_t(profit_base));
  pr.fee_lamports = cfg.base_fee_lamports + uint64_t(u128(cfg.cu_price_micro_lamports) * cu / 1'000'000);
  uint64_t tip_min = std::max(cfg.tip_min_lamports, tips.tip_floor_p25());
  uint64_t tip = uint64_t(u128(pr.gross_lamports) * tips.alpha_ppm(route_class) / 1'000'000);
  tip = std::clamp(tip, tip_min, std::max(tip_min, cfg.tip_max_lamports));
  // Never let the tip push net below min_net (§6.6).
  uint64_t reserved = pr.fee_lamports + cfg.min_net_lamports;
  if (pr.gross_lamports <= reserved || pr.gross_lamports - reserved < tip_min) { pr.reject = Reason::below_min_net; return pr; }
  tip = std::min(tip, pr.gross_lamports - reserved);
  pr.tip_lamports = tip;
  pr.net_lamports = int64_t(pr.gross_lamports) - int64_t(tip) - int64_t(pr.fee_lamports);
  pr.p_land_ppm = tips.p_land_ppm(route_class);
  __int128 ev = (__int128(pr.p_land_ppm) * pr.net_lamports - __int128(1'000'000 - pr.p_land_ppm) * cfg.fail_cost_lamports) / 1'000'000;
  pr.ev_lamports = int64_t(ev);
  uint64_t guard = bp.from_lamports_ceil(tip);
  uint64_t floor = bp.from_lamports_ceil(cfg.min_net_lamports);
  u128 min_out = u128(x) + flash_fee + guard + floor;
  if (min_out > UINT64_MAX) { pr.reject = Reason::guard_overflow; return pr; }
  // Rounding the tip and floor up into base units must not demand more than the quote delivers.
  if (min_out > u128(x) + flash_fee + uint64_t(profit_base)) { pr.reject = Reason::guard_above_quote; return pr; }
  pr.min_out = uint64_t(min_out);
  if (pr.ev_lamports < int64_t(cfg.min_ev_lamports)) { pr.reject = Reason::below_min_ev; return pr; }
  pr.ok = true;
  return pr;
}

}  // namespace arb::engine
