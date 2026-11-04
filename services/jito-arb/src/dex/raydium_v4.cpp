// Raydium AMM v4 swap_base_in, ported from raydium-io/raydium-amm@91739d8:
//   program/src/processor.rs process_swap_base_in / process_swap_base_in_v2
//   program/src/math.rs Calculator::calc_total_without_take_pnl_no_orderbook, swap_token_amount_base_in,
//                       CheckedCeilDiv
#include "core/programs.hpp"
#include "dex/quoter.hpp"

namespace arb::dex {

Result<Quote> RaydiumV4Quoter::quote_exact_in(const PoolState& p, bool a_to_b, uint64_t amount_in) const {
  if (p.kind != DexKind::RaydiumV4) return fail(Errc::unsupported, "not a raydium pool");
  if (!p.complete) return fail(Errc::not_quotable, "incomplete state");
  const state::RaydiumV4& amm = p.ray;
  // WaitingTrade (7) flips to SwapOnly only after pool_open_time; without a trusted clock here, refuse it.
  if (!amm.swap_permitted() || amm.status == 7) return fail(Errc::unsupported, "pool status");
  if (amm.coin_mint == amm.pc_mint) return fail(Errc::unsupported, "same mint");
  if (amount_in == 0) return fail(Errc::zero_amount, "amount_in 0");
  // total_{pc,coin}_without_take_pnl = vault amount - need_take_pnl (checked_sub -> CheckedSubOverflow).
  // Coin is token "a" (vault_a), pc is token "b" (vault_b).
  if (p.vault_b_amount < amm.need_take_pnl_pc || p.vault_a_amount < amm.need_take_pnl_coin)
    return fail(Errc::overflow, "need_take_pnl exceeds vault");
  u128 total_pc = p.vault_b_amount - amm.need_take_pnl_pc;
  u128 total_coin = p.vault_a_amount - amm.need_take_pnl_coin;
  // swap_fee = ceil(amount_in * swap_fee_numerator / swap_fee_denominator)
  u128 num = u128(amount_in) * amm.swap_fee_numerator;
  u128 den = amm.swap_fee_denominator;
  u128 swap_fee = num / den + (num % den != 0 ? 1 : 0);
  u128 in_after_fee = u128(amount_in) - swap_fee;
  // Coin2PC: out = pc * in / (coin + in); PC2Coin: out = coin * in / (pc + in) (floor).
  u128 out = a_to_b ? total_pc * in_after_fee / (total_coin + in_after_fee) : total_coin * in_after_fee / (total_pc + in_after_fee);
  if (out == 0) return fail(Errc::zero_amount, "swap_amount_out 0");  // InvalidInput
  if (a_to_b ? out >= total_pc : out >= total_coin) return fail(Errc::insufficient_liquidity, "InsufficientFunds");
  Quote q;
  q.amount_in = amount_in;
  q.amount_out = uint64_t(out);
  q.fee_paid = uint64_t(swap_fee);
  q.est_compute_units = cu_;
  return q;
}

}  // namespace arb::dex
