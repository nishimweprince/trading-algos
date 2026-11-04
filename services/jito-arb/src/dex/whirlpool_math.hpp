#pragma once
// Whirlpool fixed-point math, ported verbatim from orca-so/whirlpools@cc683ec:
//   programs/whirlpool/src/math/{tick_math,token_math,swap_math,bit_math,u256_math}.rs
// Only the exact-input path the bot uses is ported; behaviour on every branch
// (rounding, the AmountDeltaU64::ExceedsMax shortcut, overflow errors) follows the source.
#include <cstdint>

#include "core/bytes.hpp"
#include "core/error.hpp"

namespace arb::dex::wp {

/** 79226673515401279992447579055 (tick_math.rs MAX_SQRT_PRICE_X64). */
constexpr u128 MAX_SQRT_PRICE_X64 = (u128(4294886577ULL) << 64) | u128(3871828160200520623ULL);
constexpr u128 MIN_SQRT_PRICE_X64 = 4295048016ULL;
constexpr u128 FEE_RATE_MUL_VALUE = 1'000'000;
constexpr u128 PROTOCOL_FEE_RATE_MUL_VALUE = 10'000;
constexpr u128 Q64_MASK = 0xFFFFFFFFFFFFFFFFULL;

u128 sqrt_price_from_tick_index(int32_t tick);
int32_t tick_index_from_sqrt_price(u128 sqrt_price_x64);

/** try_get_amount_delta_{a,b}: `exceeds` mirrors AmountDeltaU64::ExceedsMax; an Err is a program error. */
struct Delta {
  uint64_t value = 0;
  bool exceeds = false;
};
Result<Delta> try_get_amount_delta_a(u128 sqrt_price_0, u128 sqrt_price_1, u128 liquidity, bool round_up);
Result<Delta> try_get_amount_delta_b(u128 sqrt_price_0, u128 sqrt_price_1, u128 liquidity, bool round_up);
Result<uint64_t> get_amount_delta_a(u128 sqrt_price_0, u128 sqrt_price_1, u128 liquidity, bool round_up);
Result<uint64_t> get_amount_delta_b(u128 sqrt_price_0, u128 sqrt_price_1, u128 liquidity, bool round_up);
Result<u128> get_next_sqrt_price(u128 sqrt_price, u128 liquidity, uint64_t amount, bool amount_specified_is_input, bool a_to_b);

struct SwapStep {
  uint64_t amount_in = 0, amount_out = 0, fee_amount = 0;
  u128 next_price = 0;
};

/** swap_math.rs compute_swap (exact-in and exact-out, as ported). */
Result<SwapStep> compute_swap(uint64_t amount_remaining, uint32_t fee_rate, u128 liquidity, u128 sqrt_price_current,
                              u128 sqrt_price_target, bool amount_specified_is_input, bool a_to_b);

}  // namespace arb::dex::wp
