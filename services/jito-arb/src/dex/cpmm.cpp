// Raydium CPMM swap_base_input, ported from raydium-io/raydium-cp-swap@3d69122:
//   programs/cp-swap/src/instructions/swap_base_input.rs
//   programs/cp-swap/src/states/pool.rs  vault_amount_without_fee, is_creator_fee_on_input, adjust_creator_fee_rate
//   programs/cp-swap/src/curve/calculator.rs CurveCalculator::swap_base_input
//   programs/cp-swap/src/curve/fees.rs  trading_fee (ceil), creator_fee (ceil), split_creator_fee (floor)
//   programs/cp-swap/src/curve/constant_product.rs swap_base_input_without_fees (floor)
// Token-2022 transfer fees are not modelled: supports() refuses pools whose mints charge one.
#include "dex/quoter.hpp"

namespace arb::dex {

namespace {

constexpr u128 FEE_DEN = 1'000'000;

u128 ceil_div(u128 amount, u128 num, u128 den) { return (amount * num + den - 1) / den; }

bool charges_transfer_fee(const std::shared_ptr<const state::Mint>& m) {
  return m && m->has_transfer_fee && (m->older_fee.basis_points != 0 || m->newer_fee.basis_points != 0);
}

}  // namespace

bool CpmmQuoter::supports(const PoolState& p) const {
  return p.kind == DexKind::Cpmm && !charges_transfer_fee(p.mint_a_info) && !charges_transfer_fee(p.mint_b_info);
}

Result<Quote> CpmmQuoter::quote_exact_in(const PoolState& p, bool a_to_b, uint64_t amount_in) const {
  if (p.kind != DexKind::Cpmm) return fail(Errc::unsupported, "not a cpmm pool");
  if (!p.complete) return fail(Errc::not_quotable, "incomplete state");
  if (!p.cpmm.swap_enabled()) return fail(Errc::unsupported, "swap disabled");  // NotApproved
  if (!supports(p)) return fail(Errc::unsupported, "transfer fee mint");
  if (amount_in == 0) return fail(Errc::zero_amount, "amount_in 0");
  uint64_t rin = 0, rout = 0;
  if (!cp_reserves(p, a_to_b, rin, rout)) return fail(Errc::overflow, "InsufficientVault");
  const state::CpmmConfig& c = p.cpmm_cfg;
  const bool on_input = p.cpmm.creator_fee_on_input(a_to_b);  // token "a" is token_0: a_to_b = ZeroForOne
  const u128 trade_rate = c.trade_fee_rate, creator_rate = p.cpmm.enable_creator_fee ? c.creator_fee_rate : 0;
  const u128 x = amount_in;
  u128 fee = 0, less = 0;
  if (on_input) {
    if (trade_rate + creator_rate == 0) return fail(Errc::div_by_zero, "ZeroTradingTokens");  // split_creator_fee: None
    fee = ceil_div(x, trade_rate + creator_rate, FEE_DEN);
    if (fee > x) return fail(Errc::overflow, "fee exceeds input");
    less = x - fee;
  } else {
    fee = ceil_div(x, trade_rate, FEE_DEN);
    if (fee > x) return fail(Errc::overflow, "fee exceeds input");
    less = x - fee;
  }
  if (u128(rin) + less == 0) return fail(Errc::div_by_zero, "empty pool");
  u128 swapped = less * u128(rout) / (u128(rin) + less);
  u128 out = swapped;
  if (!on_input) {
    u128 creator_fee = ceil_div(swapped, creator_rate, FEE_DEN);
    if (creator_fee > swapped) return fail(Errc::overflow, "creator fee exceeds output");
    out = swapped - creator_fee;
  }
  if (out == 0) return fail(Errc::zero_amount, "amount_received 0");
  if (swapped > rout) return fail(Errc::insufficient_liquidity, "output exceeds vault");
  Quote q;
  q.amount_in = amount_in;
  q.amount_out = uint64_t(out);
  q.fee_paid = uint64_t(fee);
  q.est_compute_units = cu_;
  return q;
}

bool cp_reserves(const PoolState& p, bool a_to_b, uint64_t& rin, uint64_t& rout) {
  uint64_t a = p.vault_a_amount, b = p.vault_b_amount, owed_a = 0, owed_b = 0;
  switch (p.kind) {
    case DexKind::RaydiumV4:
      owed_a = p.ray.need_take_pnl_coin;
      owed_b = p.ray.need_take_pnl_pc;
      break;
    case DexKind::Cpmm:
      owed_a = p.cpmm.fees_owed_0();
      owed_b = p.cpmm.fees_owed_1();
      break;
    default:
      return false;
  }
  if (owed_a > a || owed_b > b) return false;
  a -= owed_a;
  b -= owed_b;
  rin = a_to_b ? a : b;
  rout = a_to_b ? b : a;
  return true;
}

}  // namespace arb::dex
