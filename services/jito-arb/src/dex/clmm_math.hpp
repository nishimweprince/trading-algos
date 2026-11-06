#pragma once
// Raydium CLMM math, ported from raydium-clmm@6d7a0f8 programs/amm/src/libraries:
//   tick_math.rs          get_sqrt_price_at_tick, get_tick_at_sqrt_price (MIN/MAX_SQRT_PRICE_X64)
//   tick_array_bit_map.rs check_current_tick_array_is_initialized, next_initialized_tick_array_start_index
//   sqrt_price_math.rs    get_next_sqrt_price_from_input (amount 0 rounding up, amount 1 rounding down)
//   liquidity_math.rs     get_delta_amounts_for_swap, add_delta
//   swap_math.rs          compute_swap (base input; fee on input or on output)
// Products reach 2^320, so mul-div runs on 512-bit integers and is exact.
#include <cstdint>

#include "core/bytes.hpp"
#include "state/accounts.hpp"
#include "core/error.hpp"

namespace arb::dex::clmm {

constexpr int32_t MIN_TICK = -443636;
constexpr int32_t MAX_TICK = 443636;
inline constexpr u128 MIN_SQRT_PRICE_X64 = 4295048016;
inline constexpr u128 MAX_SQRT_PRICE_X64 = (u128(0xfffec4b1ULL) << 64) | u128(0x845c1aa94e69579bULL);  // 79226673521066979257578248091
constexpr uint32_t FEE_RATE_DENOMINATOR = 1'000'000;
constexpr int32_t TICK_ARRAY_SIZE = 60;
constexpr int32_t TICK_ARRAY_BITMAP_SIZE = 512;

Result<u128> sqrt_price_at_tick(int32_t tick);
Result<int32_t> tick_at_sqrt_price(u128 sqrt_price_x64);

int32_t tick_count(uint16_t tick_spacing);
int32_t array_start_index(int32_t tick, uint16_t tick_spacing);
/** Ticks the pool's own 1024-bit bitmap covers: [-max, max). */
int32_t max_tick_in_bitmap(uint16_t tick_spacing);
/** The pool's default bitmap range for array start indexes (tick_array_start_index_range). */
void bitmap_start_range(uint16_t tick_spacing, int32_t& min_start, int32_t& max_start);
/** check_current_tick_array_is_initialized: (initialized, start of the current tick's array). */
bool current_array_initialized(const uint64_t bitmap[16], int32_t tick_current, uint16_t tick_spacing, int32_t& start);
/** next_initialized_tick_array_start_index (default bitmap only): found, and the next start or the boundary. */
bool next_initialized_array(const uint64_t bitmap[16], int32_t last_start, uint16_t tick_spacing, bool zero_for_one, int32_t& out);

/** Search initialized arrays over the entire legal tick range. Missing extension is a cache gap.
 * include_current is used for the first array; subsequent searches exclude last_start. */
Result<int32_t> initialized_array(const state::ClmmPool& pool, const state::ClmmBitmapExtension* extension,
                                  int32_t last_start, bool zero_for_one, bool include_current = false);

struct StepResult {
  u128 sqrt_price_next_x64 = 0;
  uint64_t amount_in = 0, amount_out = 0, fee_amount = 0;
};
/** compute_swap for an exact-input step. */
Result<StepResult> compute_swap_exact_in(u128 sqrt_current, u128 sqrt_target, u128 liquidity, uint64_t amount_remaining,
                                         uint32_t fee_rate, bool zero_for_one, bool fee_on_input);

/** liquidity_math::add_delta (LiquiditySubValueErr when it would not decrease strictly). */
Result<u128> add_delta(u128 x, i128 y);

}  // namespace arb::dex::clmm
