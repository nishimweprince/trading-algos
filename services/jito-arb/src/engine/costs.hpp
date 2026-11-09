#pragma once
// Cost model (spec §6.5), tip policy (§6.6) and the on-chain guard value (§6.8). All values are
// integers: lamports, base-token raw units, and parts-per-million for alpha and p_land.
#include <cstdint>
#include <map>

#include "core/bytes.hpp"
#include "core/types.hpp"

namespace arb::engine {

/** Base-token value in lamports, Q64.64 lamports per raw unit (wSOL = 1.0). */
struct BasePrice {
  u128 lamports_per_unit_x64 = u128(1) << 64;
  bool known = true;
  uint64_t to_lamports(uint64_t units) const;
  /** Lamports -> base units, rounded up (used for guard amounts, which must cover the cost). */
  uint64_t from_lamports_ceil(uint64_t lamports) const;
};

struct CostConfig {
  uint64_t base_fee_lamports = 5000;  // one signature
  uint64_t cu_price_micro_lamports = 0;
  uint64_t min_net_lamports = 10'000;
  uint64_t min_ev_lamports = 20'000;
  uint64_t tip_min_lamports = 1000;
  uint64_t tip_max_lamports = 50'000'000;
  uint32_t cu_overhead = 60'000;
  uint32_t cu_headroom_ppm = 150'000;
  uint32_t max_route_cu = 1'200'000;
  uint64_t fail_cost_lamports = 0;  // bundle path: a bundle that does not land costs nothing
};

/**
 * Tip controller per route class: tip = clamp(alpha_k * gross, tip_min, tip_max) where tip_min is
 * max(config floor, Jito's 25th-percentile tip floor). Every `window` attempts in a class alpha moves
 * by `step` toward the target landing rate, bounded to [alpha_min, alpha_max].
 */
class TipPolicy {
 public:
  struct Params {
    uint32_t alpha_init_ppm = 500'000, alpha_min_ppm = 200'000, alpha_max_ppm = 950'000, step_ppm = 50'000;
    uint32_t target_land_ppm = 300'000;
    uint32_t window = 50;
    uint32_t p_land_init_ppm = 100'000;
  };
  TipPolicy() = default;
  explicit TipPolicy(Params p) : p_(p) {}
  uint32_t alpha_ppm(uint16_t route_class) const;
  /** Landing probability from the class's own history (Laplace-smoothed toward p_land_init). */
  uint32_t p_land_ppm(uint16_t route_class) const;
  void set_tip_floor_p25(uint64_t lamports) { floor_p25_ = lamports; }
  uint64_t tip_floor_p25() const { return floor_p25_; }
  /** Record one attempt's outcome (landed or not) and run the window adjustment. */
  void record(uint16_t route_class, bool landed);

 private:
  struct Class {
    uint32_t alpha_ppm = 0;
    uint64_t attempts = 0, landed = 0;
    uint32_t window_attempts = 0, window_landed = 0;
  };
  Class& cls(uint16_t rc);
  Params p_;
  std::map<uint16_t, Class> classes_;
  uint64_t floor_p25_ = 0;
};

/** Why a candidate was not sent (decision log `reason`; values are the on-disk encoding). */
enum class Reason : uint8_t {
  none = 0, base_price_unknown, no_profit, cu_over_budget, below_min_net, guard_overflow, guard_above_quote,
  below_min_ev, pool_conflict, slot_cap, send_lag, sizer_failed, boundary, flash_disabled, risk_block,
  tx_build_failed, sign_guard,
  // Why the sizer found no profitable size (appended: the values above are the on-disk encoding).
  sizer_no_room, sizer_no_profit, sizer_partial_fill, sizer_quote_error, sizer_zero_out,
  // Appended: a sized profit too large to be real (stale or broken pool state), and a pool direction the
  // bundle simulations keep rejecting.
  implausible_profit, quarantined,
};
const char* reason_name(Reason r);

struct Pricing {
  bool ok = false;
  Reason reject = Reason::none;
  uint64_t gross_lamports = 0;   // profit after the flash fee, before tip and fees
  uint64_t tip_lamports = 0;
  uint64_t fee_lamports = 0;     // base fee + priority fee
  int64_t net_lamports = 0;
  int64_t ev_lamports = 0;
  uint64_t min_out = 0;          // §6.8 guard, base units
  uint32_t cu_limit = 0;
  uint32_t p_land_ppm = 0;
};

/**
 * Price one sized cycle: costs, tip and EV in lamports, and the executor's min_out:
 *   min_out = x + flash_fee + tip_in_base + min_net_in_base   (tip and floor rounded up in base units)
 */
Pricing price(uint64_t x, int64_t profit_base, uint64_t flash_fee, uint32_t route_cu, uint16_t route_class,
              const BasePrice& bp, const TipPolicy& tips, const CostConfig& cfg);

}  // namespace arb::engine
