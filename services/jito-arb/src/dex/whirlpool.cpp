// Whirlpool exact-in quote: the swap loop of programs/whirlpool/src/manager/swap_manager.rs (static fee
// rate path of FeeRateManager), the tick-array sequence of util/sparse_swap.rs (get_start_tick_indexes,
// uninitialized PDAs as zeroed arrays) and util/swap_tick_sequence.rs, and the tick-array search of
// state/{fixed,dynamic}_tick_array.rs. orca-so/whirlpools@cc683ec.
#include <algorithm>

#include "core/u256.hpp"
#include "dex/quoter.hpp"
#include "dex/whirlpool_math.hpp"

namespace arb::dex {

namespace {

using state::MAX_TICK_INDEX;
using state::MIN_TICK_INDEX;
using state::TICK_ARRAY_SIZE;

int32_t floor_div(int32_t a, int32_t b) {
  int32_t q = a / b;
  return (a % b != 0 && ((a < 0) != (b < 0))) ? q - 1 : q;
}

bool check_is_out_of_bounds(int32_t t) { return t < MIN_TICK_INDEX || t > MAX_TICK_INDEX; }

// Tick::check_is_valid_start_tick
bool valid_start_tick(int32_t tick_index, uint16_t spacing) {
  int32_t ticks_in_array = TICK_ARRAY_SIZE * int32_t(spacing);
  if (check_is_out_of_bounds(tick_index)) {
    if (tick_index > MIN_TICK_INDEX) return false;
    int32_t min_array_start_index = MIN_TICK_INDEX - (MIN_TICK_INDEX % ticks_in_array + ticks_in_array);
    return tick_index == min_array_start_index;
  }
  return tick_index % ticks_in_array == 0;
}

bool usable_tick(int32_t t, uint16_t spacing) { return !check_is_out_of_bounds(t) && t % int32_t(spacing) == 0; }

/** One element of the SwapTickSequence: a loaded array or a zeroed (uninitialized PDA) one. */
struct SeqArray {
  int32_t start = 0;
  const state::TickArray* data = nullptr;  // null = ZeroedTickArray

  bool initialized(int offset) const { return data && data->ticks[size_t(offset)].initialized; }
  bool in_search_range(int32_t tick, uint16_t spacing, bool shifted) const {
    int32_t lower = start, upper = start + TICK_ARRAY_SIZE * int32_t(spacing);
    if (shifted) {
      lower -= spacing;
      upper -= spacing;
    }
    return tick >= lower && tick < upper;
  }
  int32_t offset(int32_t tick, uint16_t spacing) const {  // get_offset
    int32_t lhs = tick - start, rhs = int32_t(spacing);
    int32_t d = lhs / rhs, r = lhs % rhs;
    return r < 0 ? d - 1 : d;
  }
  bool is_min() const { return start <= MIN_TICK_INDEX; }
  bool is_max(uint16_t spacing) const { return start + TICK_ARRAY_SIZE * int32_t(spacing) > MAX_TICK_INDEX; }
};

struct Seq {
  SeqArray arrays[3];
  int n = 0;
  bool truncated_by_cache = false;  // a required array exists on chain but is not cached
};

// sparse_swap.rs get_start_tick_indexes + try_build: up to three arrays in swap direction; a PDA with no
// account is a zeroed array; the sequence stops at the first array the bot does not hold.
Seq build_sequence(const PoolState& p, bool a_to_b) {
  Seq s;
  uint16_t spacing = p.wp.tick_spacing;
  int32_t ticks_in_array = TICK_ARRAY_SIZE * int32_t(spacing);
  int32_t base = floor_div(p.wp.tick_current_index, ticks_in_array) * ticks_in_array;
  int offs[3];
  if (a_to_b) {
    offs[0] = 0; offs[1] = -1; offs[2] = -2;
  } else {
    bool shifted = p.wp.tick_current_index + int32_t(spacing) >= base + ticks_in_array;
    for (int i = 0; i < 3; ++i) offs[i] = shifted ? i + 1 : i;
  }
  for (int o : offs) {
    int32_t start = base + o * ticks_in_array;
    if (!valid_start_tick(start, spacing)) continue;  // filter_map
    const TickArraySlot* slot = p.tick_slot(start);
    if (!slot || slot->state == TickArraySlot::Unknown) {
      s.truncated_by_cache = true;
      break;
    }
    s.arrays[s.n++] = SeqArray{start, slot->state == TickArraySlot::Loaded ? slot->array.get() : nullptr};
  }
  return s;
}

enum class SeqErr { ok, invalid_index, invalid_sequence };

// TickArrayType::get_next_init_tick_index
SeqErr next_init_in_array(const SeqArray& a, int32_t tick, uint16_t spacing, bool a_to_b, bool& found, int32_t& out) {
  found = false;
  if (!a.in_search_range(tick, spacing, !a_to_b)) return SeqErr::invalid_sequence;
  int32_t off = a.offset(tick, spacing);
  if (!a_to_b) off += 1;
  while (off >= 0 && off < TICK_ARRAY_SIZE) {
    if (a.initialized(off)) {
      found = true;
      out = off * int32_t(spacing) + a.start;
      return SeqErr::ok;
    }
    off = a_to_b ? off - 1 : off + 1;
  }
  return SeqErr::ok;
}

// SwapTickSequence::get_next_initialized_tick_index
SeqErr next_initialized_tick(const Seq& s, int32_t tick_index, uint16_t spacing, bool a_to_b, int start_array,
                             int& array_out, int32_t& tick_out) {
  int32_t ticks_in_array = TICK_ARRAY_SIZE * int32_t(spacing);
  int32_t search = tick_index;
  int ai = start_array;
  for (;;) {
    if (ai >= s.n) return SeqErr::invalid_index;
    const SeqArray& a = s.arrays[ai];
    bool found;
    int32_t next;
    SeqErr e = next_init_in_array(a, search, spacing, a_to_b, found, next);
    if (e != SeqErr::ok) return e;
    if (found) {
      array_out = ai;
      tick_out = next;
      return SeqErr::ok;
    }
    if (a_to_b && a.is_min()) { array_out = ai; tick_out = MIN_TICK_INDEX; return SeqErr::ok; }
    if (!a_to_b && a.is_max(spacing)) { array_out = ai; tick_out = MAX_TICK_INDEX; return SeqErr::ok; }
    if (ai + 1 == s.n) {
      array_out = ai;
      tick_out = a_to_b ? a.start : a.start + ticks_in_array - 1;
      return SeqErr::ok;
    }
    search = a_to_b ? a.start - 1 : a.start + ticks_in_array - 1;
    ++ai;
  }
}

}  // namespace

Result<Quote> WhirlpoolQuoter::quote_exact_in(const PoolState& p, bool a_to_b, uint64_t amount) const {
  if (p.kind != DexKind::Whirlpool) return fail(Errc::unsupported, "not a whirlpool");
  if (!p.complete) return fail(Errc::not_quotable, "incomplete state");
  const state::Whirlpool& w = p.wp;
  if (w.adaptive_fee()) return fail(Errc::unsupported, "adaptive fee pool");
  if (amount == 0) return fail(Errc::zero_amount, "ZeroTradableAmount");
  const u128 limit = a_to_b ? wp::MIN_SQRT_PRICE_X64 : wp::MAX_SQRT_PRICE_X64;  // NO_EXPLICIT_SQRT_PRICE_LIMIT
  if ((a_to_b && limit >= w.sqrt_price) || (!a_to_b && limit <= w.sqrt_price))
    return fail(Errc::bad_data, "InvalidSqrtPriceLimitDirection");
  Seq seq = build_sequence(p, a_to_b);
  Quote q;
  q.est_compute_units = cu_;
  if (seq.n == 0) {  // InvalidTickArraySequence (or nothing cached)
    q.crosses_boundary = true;
    q.cache_gap = seq.truncated_by_cache;
    return q;
  }
  const uint16_t spacing = w.tick_spacing;
  const uint32_t fee_rate = w.fee_rate;
  uint64_t amount_remaining = amount, amount_calculated = 0, fee_sum = 0;
  u128 curr_sqrt_price = w.sqrt_price;
  int32_t curr_tick_index = w.tick_current_index;
  u128 curr_liquidity = w.liquidity;
  int curr_array_index = 0;
  while (amount_remaining > 0 && limit != curr_sqrt_price) {
    int next_array_index = 0;
    int32_t next_tick_index = 0;
    SeqErr e = next_initialized_tick(seq, curr_tick_index, spacing, a_to_b, curr_array_index, next_array_index, next_tick_index);
    if (e != SeqErr::ok) {
      // The program fails (TickArraySequenceInvalidIndex / InvalidTickArraySequence): the swap needs more
      // tick arrays than the transaction can carry or the bot holds. Discard this size.
      q.crosses_boundary = true;
      // Out of arrays: a cache gap if the sequence was cut short by an array the bot does not hold, otherwise
      // the program's 3-array window is used up (the chain would fail the same way).
      q.cache_gap = seq.truncated_by_cache;
      q.amount_in = amount - amount_remaining;
      q.amount_out = amount_calculated;
      q.fee_paid = fee_sum;
      return q;
    }
    u128 next_tick_sqrt_price = wp::sqrt_price_from_tick_index(next_tick_index);
    u128 target = a_to_b ? (limit > next_tick_sqrt_price ? limit : next_tick_sqrt_price)
                         : (limit < next_tick_sqrt_price ? limit : next_tick_sqrt_price);
    // Static fee: the inner do-while runs once (bounded target == target, no adaptive skip).
    auto step = wp::compute_swap(amount_remaining, fee_rate, curr_liquidity, curr_sqrt_price, target, true, a_to_b);
    if (!step) return std::unexpected(step.error());
    if (amount_remaining < step->amount_in) return fail(Errc::overflow, "AmountRemainingOverflow");
    amount_remaining -= step->amount_in;
    if (amount_remaining < step->fee_amount) return fail(Errc::overflow, "AmountRemainingOverflow");
    amount_remaining -= step->fee_amount;
    if (amount_calculated > UINT64_MAX - step->amount_out) return fail(Errc::overflow, "AmountCalcOverflow");
    amount_calculated += step->amount_out;
    fee_sum += step->fee_amount;
    if (step->next_price == next_tick_sqrt_price) {
      const SeqArray& arr = seq.arrays[next_array_index];
      // get_tick: TickNotFound (out of array bounds / not usable) reads as uninitialized.
      bool in_bounds = arr.in_search_range(next_tick_index, spacing, false) && usable_tick(next_tick_index, spacing);
      int32_t off = arr.offset(next_tick_index, spacing);
      if (in_bounds && off >= 0 && arr.initialized(off)) {
        i128 net = arr.data->ticks[size_t(off)].liquidity_net;
        i128 signed_net = a_to_b ? -net : net;
        if (signed_net > 0) {
          if (curr_liquidity > ~u128(0) - u128(signed_net)) return fail(Errc::overflow, "LiquidityOverflow");
          curr_liquidity += u128(signed_net);
        } else if (signed_net < 0) {
          u128 mag = u128(-signed_net);
          if (curr_liquidity < mag) return fail(Errc::overflow, "LiquidityUnderflow");
          curr_liquidity -= mag;
        }
      }
      curr_array_index = ((a_to_b && off == 0) || (!a_to_b && off == TICK_ARRAY_SIZE - 1)) ? next_array_index + 1
                                                                                            : next_array_index;
      curr_tick_index = a_to_b ? next_tick_index - 1 : next_tick_index;
    } else if (step->next_price != curr_sqrt_price) {
      curr_tick_index = wp::tick_index_from_sqrt_price(step->next_price);
    }
    curr_sqrt_price = step->next_price;
  }
  q.amount_in = amount - amount_remaining;
  q.amount_out = amount_calculated;
  q.fee_paid = fee_sum;
  return q;
}

u128 spot_rate_x64(const PoolState& p, bool a_to_b) {
  if (p.kind == DexKind::Cpmm) {
    uint64_t rin = 0, rout = 0;
    if (!cp_reserves(p, a_to_b, rin, rout) || rin == 0) return 0;
    const u128 den = 1'000'000;
    u128 trade = p.cpmm_cfg.trade_fee_rate, creator = p.cpmm.enable_creator_fee ? p.cpmm_cfg.creator_fee_rate : 0;
    // Creator fee on the input adds to the input fee; on the output it scales the output.
    u128 keep = p.cpmm.creator_fee_on_input(a_to_b) ? den - std::min(den, trade + creator)
                                                    : (den - std::min(den, trade)) * (den - std::min(den, creator)) / den;
    u128 rate = (u128(rout) << 64) / rin;
    U256 qq, rr;
    U256::mul(rate, keep).divmod(U256::from_u128(den), qq, rr);
    return qq.fits_u128() ? qq.lo128() : ~u128(0);
  }
  if (p.kind == DexKind::PumpSwap) {
    uint64_t rin = 0, rout = 0;
    if (!cp_reserves(p, a_to_b, rin, rout) || rin == 0) return 0;
    uint64_t base = a_to_b ? rin : rout, quote = a_to_b ? rout : rin;
    auto bps = pump_fee_bps(p, base, quote);
    if (!bps) return 0;
    u128 total = u128(bps->lp) + bps->protocol + (is_zero(p.pump.coin_creator) ? 0 : bps->creator);
    u128 keep = 10'000 - std::min<u128>(10'000, total);
    u128 rate = (u128(rout) << 64) / rin;
    U256 qq, rr;
    U256::mul(rate, keep).divmod(U256::from_u128(10'000), qq, rr);
    return qq.fits_u128() ? qq.lo128() : ~u128(0);
  }
  if (p.kind != DexKind::RaydiumV4 && p.kind != DexKind::Whirlpool) return 0;  // no spot: the prefilter skips the cycle
  if (p.kind == DexKind::RaydiumV4) {
    u128 rin = a_to_b ? p.vault_a_amount - std::min(p.vault_a_amount, p.ray.need_take_pnl_coin)
                      : p.vault_b_amount - std::min(p.vault_b_amount, p.ray.need_take_pnl_pc);
    u128 rout = a_to_b ? p.vault_b_amount - std::min(p.vault_b_amount, p.ray.need_take_pnl_pc)
                       : p.vault_a_amount - std::min(p.vault_a_amount, p.ray.need_take_pnl_coin);
    if (rin == 0 || p.ray.swap_fee_denominator == 0) return 0;
    // (rout / rin) * (1 - f), Q64.64. rout < 2^64 so rout << 64 fits.
    u128 rate = (rout << 64) / rin;
    u128 keep = p.ray.swap_fee_denominator - p.ray.swap_fee_numerator;
    U256 qq, rr;
    U256::mul(rate, keep).divmod(U256::from_u128(p.ray.swap_fee_denominator), qq, rr);
    return qq.fits_u128() ? qq.lo128() : ~u128(0);
  }
  // Whirlpool: price b per a = sqrt^2 / 2^128. a->b gives price, b->a gives 1/price.
  u128 s = p.wp.sqrt_price;
  if (s == 0) return 0;
  U256 sq = U256::mul(s, s);  // Q128.128
  u128 rate;
  if (a_to_b) {
    rate = sq.shr(64).fits_u128() ? sq.shr(64).lo128() : ~u128(0);
  } else {
    // 2^192 / s^2, Q64.64
    U256 one = U256::from_words(0, 1, 0, 0).shl64().shl64();  // 2^192
    U256 qq, rr;
    one.divmod(sq, qq, rr);
    rate = qq.fits_u128() ? qq.lo128() : ~u128(0);
  }
  u128 keep = wp::FEE_RATE_MUL_VALUE - p.wp.fee_rate;
  U256 r = U256::mul(rate, keep);
  U256 qq, rr;
  r.divmod(U256::from_u128(wp::FEE_RATE_MUL_VALUE), qq, rr);
  return qq.fits_u128() ? qq.lo128() : ~u128(0);
}

uint64_t input_depth(const PoolState& p, bool a_to_b) {
  if (is_constant_product(p.kind)) {
    uint64_t rin = 0, rout = 0;
    if (cp_reserves(p, a_to_b, rin, rout)) return rin;
    return a_to_b ? p.vault_a_amount : p.vault_b_amount;
  }
  if (p.kind != DexKind::Whirlpool) return 0;
  // Concentrated liquidity: the vaults also hold positions far from the current price, so their balances
  // overstate what a swap can use. Depth is the input that moves sqrt-price 5% (about 10% in price) inside the
  // active liquidity L, from the CLMM identities (sqrt-price s in Q64.64):
  //   a in (s falls to s(1 - d)):  da = L * 2^64 * d / (s * (1 - d))   d = 5%: L * 2^64 / s / 19
  //   b in (s rises to s(1 + d)):  db = L * s * d / 2^64                       L * s / 2^64 / 20
  // A stranded pool (little L near a far-off price) gets a small depth and so a small x_max.
  const u128 L = p.wp.liquidity, sq = p.wp.sqrt_price;
  if (L == 0 || sq == 0) return 0;
  U256 v, t, r;
  if (a_to_b) {
    U256::mul(L, u128(1) << 64).divmod(U256::from_u128(sq), t, r);
    t.divmod(U256::from_u128(19), v, r);
  } else {
    U256::mul(L, sq).shr(64).divmod(U256::from_u128(20), v, r);
  }
  return v.fits_u128() && v.lo128() <= UINT64_MAX ? uint64_t(v.lo128()) : UINT64_MAX;
}

}  // namespace arb::dex
