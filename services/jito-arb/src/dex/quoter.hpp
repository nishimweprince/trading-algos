#pragma once
// Exact quoters (spec §5). Integer math only; each reproduces its program's
// output to the last unit, including fee rounding.
#include <cstdint>
#include <memory>

#include "core/error.hpp"
#include "state/pool_state.hpp"

namespace arb::dex {

struct Quote {
  uint64_t amount_in = 0;   // actually consumed
  uint64_t amount_out = 0;  // exactly what the program will transfer out
  uint64_t fee_paid = 0;    // in input-token units
  uint32_t est_compute_units = 0;
  bool crosses_boundary = false;  // a tick array outside the cache (or past the program's 3-array window) would be needed
};

class IQuoter {
 public:
  virtual ~IQuoter() = default;
  virtual Result<Quote> quote_exact_in(const PoolState&, bool a_to_b, uint64_t amount_in) const = 0;
  virtual DexKind kind() const = 0;
  /** Cheap pre-check: false when this pool can never be quoted (e.g. a fee model not ported). */
  virtual bool supports(const PoolState&) const { return true; }
};

class RaydiumV4Quoter final : public IQuoter {
 public:
  explicit RaydiumV4Quoter(uint32_t cu = 40000) : cu_(cu) {}
  Result<Quote> quote_exact_in(const PoolState& p, bool a_to_b, uint64_t amount_in) const override;
  DexKind kind() const override { return DexKind::RaydiumV4; }

 private:
  uint32_t cu_;
};

class WhirlpoolQuoter final : public IQuoter {
 public:
  explicit WhirlpoolQuoter(uint32_t cu = 80000) : cu_(cu) {}
  Result<Quote> quote_exact_in(const PoolState& p, bool a_to_b, uint64_t amount_in) const override;
  DexKind kind() const override { return DexKind::Whirlpool; }
  bool supports(const PoolState& p) const override { return !p.wp.adaptive_fee(); }  // static-fee path only

 private:
  uint32_t cu_;
};

/** Marginal output per unit input at size zero, after fee, as a Q64.64 ratio (spot-edge prefilter). */
u128 spot_rate_x64(const PoolState& p, bool a_to_b);

/** Input-side depth (raw units) used by the sizer's max_input_fraction bound. */
uint64_t input_depth(const PoolState& p, bool a_to_b);

}  // namespace arb::dex
