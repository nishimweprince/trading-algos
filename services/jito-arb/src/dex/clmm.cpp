// Raydium CLMM swap_v2, exact input, ported from raydium-clmm@6d7a0f8 programs/amm/src/instructions/swap.rs
// (swap_internal, SwapState) with the math in clmm_math.cpp. Token "a" is token_0: a_to_b = zero_for_one.
// The sqrt-price limit is the program's default (MIN + 1 / MAX - 1, sqrt_price_limit_x64 = 0).
#include <algorithm>

#include "dex/clmm_math.hpp"
#include "dex/quoter.hpp"

namespace arb::dex {

namespace {

bool charges_transfer_fee(const std::shared_ptr<const state::Mint>& m) {
  return m && m->has_transfer_fee && (m->older_fee.basis_points != 0 || m->newer_fee.basis_points != 0);
}

const state::ClmmTickArray* cached(const PoolState& p, int32_t start) {
  for (uint8_t i = 0; i < p.n_ticks; ++i)
    if (p.ticks[i].start == start) return p.ticks[i].clmm.get();
  return nullptr;
}

/** TickArrayState::next_initialized_tick: the next initialized tick at or below (zero_for_one) / above the tick. */
const state::ClmmTick* next_in_array(const state::ClmmTickArray& a, int32_t tick, uint16_t spacing, bool zero_for_one) {
  if (clmm::array_start_index(tick, spacing) != a.start_tick_index) return nullptr;
  int32_t off = (tick - a.start_tick_index) / int32_t(spacing);
  if (zero_for_one) {
    for (int32_t i = off; i >= 0; --i)
      if (a.ticks[size_t(i)].initialized()) return &a.ticks[size_t(i)];
  } else {
    for (int32_t i = off + 1; i < clmm::TICK_ARRAY_SIZE; ++i)
      if (a.ticks[size_t(i)].initialized()) return &a.ticks[size_t(i)];
  }
  return nullptr;
}

const state::ClmmTick* first_in_array(const state::ClmmTickArray& a, bool zero_for_one) {
  if (zero_for_one) {
    for (int32_t i = clmm::TICK_ARRAY_SIZE - 1; i >= 0; --i)
      if (a.ticks[size_t(i)].initialized()) return &a.ticks[size_t(i)];
  } else {
    for (int32_t i = 0; i < clmm::TICK_ARRAY_SIZE; ++i)
      if (a.ticks[size_t(i)].initialized()) return &a.ticks[size_t(i)];
  }
  return nullptr;
}

Result<Quote> boundary(uint64_t amount_in, bool gap) {
  Quote q;
  q.amount_in = amount_in;
  q.crosses_boundary = true;
  q.cache_gap = gap;
  return q;
}

}  // namespace

bool ClmmQuoter::supports(const PoolState& p) const {
  if (p.kind != DexKind::Clmm || p.clmm.dynamic_fee) return false;
  if (charges_transfer_fee(p.mint_a_info) || charges_transfer_fee(p.mint_b_info)) return false;
  int32_t lo = 0, hi = 0;
  clmm::bitmap_start_range(p.clmm.tick_spacing, lo, hi);
  int32_t start = clmm::array_start_index(p.clmm.tick_current, p.clmm.tick_spacing);
  return start >= lo && start < hi;  // else the swap needs the bitmap extension account
}

Result<Quote> ClmmQuoter::quote_exact_in(const PoolState& p, bool a_to_b, uint64_t amount_in) const {
  if (p.kind != DexKind::Clmm) return fail(Errc::unsupported, "not a clmm pool");
  if (!p.complete) return fail(Errc::not_quotable, "incomplete state");
  if (!supports(p)) return fail(Errc::unsupported, "dynamic fee, transfer fee or bitmap extension");
  if (!p.clmm.swap_enabled()) return fail(Errc::unsupported, "NotApproved");
  if (amount_in == 0) return fail(Errc::zero_amount, "ZeroAmountSpecified");
  const state::ClmmPool& pool = p.clmm;
  const bool zfo = a_to_b;
  const uint16_t spacing = pool.tick_spacing;
  const u128 limit = zfo ? clmm::MIN_SQRT_PRICE_X64 + 1 : clmm::MAX_SQRT_PRICE_X64 - 1;
  if (zfo ? !(limit < pool.sqrt_price_x64) : !(limit > pool.sqrt_price_x64)) return fail(Errc::overflow, "SqrtPriceLimitOverflow");

  // first_tick_array_index_with_extension_info (default bitmap only).
  int32_t cur_start = 0;
  bool first_contains = clmm::current_array_initialized(pool.tick_array_bitmap, pool.tick_current, spacing, cur_start);
  if (!first_contains) {
    int32_t next = 0;
    if (!clmm::next_initialized_array(pool.tick_array_bitmap, clmm::array_start_index(pool.tick_current, spacing), spacing, zfo, next)) {
      if (spacing < 15) return boundary(amount_in, false);  // the program would continue in the extension
      return fail(Errc::insufficient_liquidity, "InsufficientLiquidityForDirection");
    }
    cur_start = next;
  }
  const state::ClmmTickArray* cur = cached(p, cur_start);
  if (!cur) return boundary(amount_in, true);

  const bool fee_on_input = pool.fee_on_input(zfo);
  const uint32_t fee_rate = p.clmm_cfg.trade_fee_rate;
  uint64_t remaining = amount_in, calculated = 0, fee_sum = 0;
  u128 sqrt = pool.sqrt_price_x64, liquidity = pool.liquidity;
  int32_t tick = pool.tick_current;
  while (remaining != 0 && sqrt != limit) {
    const state::ClmmTick* next = next_in_array(*cur, tick, spacing, zfo);
    if (!next) {
      if (!first_contains) {
        first_contains = true;
        next = first_in_array(*cur, zfo);
        if (!next) return fail(Errc::bad_data, "InvalidTickArray");
      } else {
        int32_t ns = 0;
        if (!clmm::next_initialized_array(pool.tick_array_bitmap, cur_start, spacing, zfo, ns)) {
          if (spacing < 15) return boundary(amount_in, false);
          return fail(Errc::insufficient_liquidity, "LiquidityInsufficient");
        }
        cur_start = ns;
        cur = cached(p, cur_start);
        if (!cur) return boundary(amount_in, true);
        next = first_in_array(*cur, zfo);
        if (!next) return fail(Errc::bad_data, "InvalidTickArray");
      }
    }
    int32_t tick_next = std::clamp(next->tick, clmm::MIN_TICK, clmm::MAX_TICK);
    auto sp = clmm::sqrt_price_at_tick(tick_next);
    if (!sp) return fail(sp.error().code, sp.error().what);
    u128 sqrt_next = *sp;
    u128 target = (zfo && sqrt_next < limit) || (!zfo && sqrt_next > limit) ? limit : sqrt_next;
    if (zfo ? (tick < tick_next || sqrt < sqrt_next) : (tick_next <= tick || sqrt_next < sqrt)) return fail(Errc::bad_data, "tick order");
    clmm::StepResult step;
    if (sqrt != target) {
      auto r = clmm::compute_swap_exact_in(sqrt, target, liquidity, remaining, fee_rate, zfo, fee_on_input);
      if (!r) return fail(r.error().code, r.error().what);
      step = *r;
      uint64_t consumed = fee_on_input ? step.amount_in + step.fee_amount : step.amount_in;
      if (consumed > remaining) return fail(Errc::overflow, "CalculateOverflow");
      remaining -= consumed;
      calculated += step.amount_out;
      fee_sum += step.fee_amount;
    } else {
      step.sqrt_price_next_x64 = target;
    }
    if (sqrt_next == step.sqrt_price_next_x64) {
      // Reached the tick: limit orders there would fill first (not modelled), then the tick is crossed.
      if (next->limit_orders) return boundary(amount_in, false);
      if (next->liquidity_gross > 0) {
        i128 net = zfo ? -next->liquidity_net : next->liquidity_net;
        auto l = clmm::add_delta(liquidity, net);
        if (!l) return fail(l.error().code, l.error().what);
        liquidity = *l;
      }
      tick = zfo ? tick_next - 1 : tick_next;
    } else if (sqrt != step.sqrt_price_next_x64) {
      auto t = clmm::tick_at_sqrt_price(step.sqrt_price_next_x64);
      if (!t) return fail(t.error().code, t.error().what);
      tick = *t;
    }
    sqrt = step.sqrt_price_next_x64;
  }
  uint64_t consumed = amount_in - remaining;
  if (consumed == 0 || calculated == 0) return fail(Errc::zero_amount, "TooSmallInputOrOutputAmount");
  Quote q;
  q.amount_in = consumed;
  q.amount_out = calculated;
  q.fee_paid = fee_sum;
  q.est_compute_units = cu_;
  return q;
}

}  // namespace arb::dex
