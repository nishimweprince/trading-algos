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
  uint64_t fee_paid = 0;    // DLMM: collected-fee token units (Y for OnlyY); otherwise input-token units
  uint32_t est_compute_units = 0;
  bool crosses_boundary = false;  // a tick array outside the cache (or past the program's 3-array window) would be needed
  bool cache_gap = false;         // ... and it is one the program would accept but the bot does not hold (vs the window)
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

class DlmmQuoter final : public IQuoter {
 public:
  explicit DlmmQuoter(uint32_t cu = 140000) : cu_(cu) {}
  Result<Quote> quote_exact_in(const PoolState&, bool, uint64_t) const override;
  DexKind kind() const override { return DexKind::Dlmm; }
  bool supports(const PoolState&) const override;
 private:
  uint32_t cu_;
};

class CpmmQuoter final : public IQuoter {
 public:
  explicit CpmmQuoter(uint32_t cu = 50000) : cu_(cu) {}
  Result<Quote> quote_exact_in(const PoolState& p, bool a_to_b, uint64_t amount_in) const override;
  DexKind kind() const override { return DexKind::Cpmm; }
  /** Token-2022 transfer fees change the amounts the program sees: not ported, so such pools are refused. */
  bool supports(const PoolState& p) const override;

 private:
  uint32_t cu_;
};

class ClmmQuoter final : public IQuoter {
 public:
  explicit ClmmQuoter(uint32_t cu = 120000) : cu_(cu) {}
  Result<Quote> quote_exact_in(const PoolState& p, bool a_to_b, uint64_t amount_in) const override;
  DexKind kind() const override { return DexKind::Clmm; }
  /**
   * Static-fee pools inside the pool's own tick-array bitmap, without Token-2022 transfer fees. Dynamic fees move
   * with the block time and limit orders change what a crossed tick pays: neither is modelled (a swap that would
   * reach a tick with orders quotes as a boundary).
   */
  bool supports(const PoolState& p) const override;

 private:
  uint32_t cu_;
};

class PumpSwapQuoter final : public IQuoter {
 public:
  explicit PumpSwapQuoter(uint32_t cu = 120000) : cu_(cu) {}
  /** a_to_b = sell (exact base in, sell_v2); b_to_a = buy with exact quote in (buy_exact_quote_in_v2). */
  Result<Quote> quote_exact_in(const PoolState& p, bool a_to_b, uint64_t amount_in) const override;
  DexKind kind() const override { return DexKind::PumpSwap; }
  /** v2 trades refuse cashback and mayhem pools; transfer-fee mints are not modelled. */
  bool supports(const PoolState& p) const override;

 private:
  uint32_t cu_;
};

/** PumpSwap fee rates (bps) for the pool's current reserves: market-cap tier, quote-mint schedule, creator override. */
struct PumpFeeBps {
  uint64_t lp = 0, protocol = 0, creator = 0;
};
Result<PumpFeeBps> pump_fee_bps(const PoolState& p, uint64_t base_reserve, uint64_t quote_reserve);

/**
 * A constant-product pool's reserves for a swap direction: vault balances less what the program does not count
 * (Raydium v4 need_take_pnl; CPMM protocol, fund and creator fees owed). False when not constant product or
 * the fees exceed the vault.
 */
bool cp_reserves(const PoolState& p, bool a_to_b, uint64_t& rin, uint64_t& rout);

/** Marginal output per unit input at size zero, after fee, as a Q64.64 ratio (spot-edge prefilter). */
u128 spot_rate_x64(const PoolState& p, bool a_to_b);

/**
 * Input-side depth (raw units) used by the sizer's max_input_fraction bound: the input vault for a
 * constant-product pool; for a Whirlpool, the input that moves sqrt-price 5% within the active liquidity.
 */
uint64_t input_depth(const PoolState& p, bool a_to_b);

}  // namespace arb::dex
