// MeteoraAg/dlmm-sdk@1532e540db41ac405dbf4c5bb2ddfceaa1f98ff1:
// commons/src/{quote.rs,extensions/{lb_pair,bin,bin_array_bitmap}.rs,math/{price_math,u64x64_math}.rs}.
#include "dex/dlmm.hpp"
#include "core/programs.hpp"
#include "core/u256.hpp"
#include <algorithm>
#include <cstdlib>

namespace arb::dex::dlmm {
namespace {
constexpr uint64_t DEN = 1'000'000'000;
u128 ceildiv(u128 n, u128 d) { return n / d + (n % d != 0); }
Result<uint64_t> convert(uint64_t amount, u128 px, bool multiply, bool up) {
  if (!px) return fail(Errc::bad_data, "dlmm zero price");
  U256 n = multiply ? U256::mul(amount, px) : U256::from_u128(u128(amount) << 64);
  U256 q, r;
  n.divmod(U256::from_u128(multiply ? ONE : px), q, r);
  if (!q.fits_u128() || q.lo128() > UINT64_MAX || (up && !r.is_zero() && q.lo128() == UINT64_MAX))
    return fail(Errc::overflow, "dlmm amount overflow");
  return uint64_t(q.lo128()) + (up && !r.is_zero());
}
const state::DlmmBinArray* array(const PoolState& p, int32_t i) {
  for (auto& a : p.dlmm_arrays) if (a.index == i && a.array && a.array->pair == p.address && a.array->index == i) return a.array.get();
  return nullptr;
}
}
int32_t array_index(int32_t id) { return id / 70 - (id % 70 < 0); }
bool initialized(const state::DlmmPair& p, const state::DlmmBitmapExtension* e, int32_t i) {
  if (i >= -512 && i < 512) { unsigned b = unsigned(i + 512); return (p.bin_array_bitmap[b / 64] >> (b % 64)) & 1; }
  if (!e || i < -6656 || i >= 6656) return false;
  unsigned b = unsigned(i >= 512 ? i - 512 : -i - 513);
  return ((i >= 512 ? e->positive : e->negative)[b / 64] >> (b % 64)) & 1;
}
bool next_array(const PoolState& p, int32_t from, bool down, int32_t& out) {
  for (int32_t i = from; i >= -6656 && i < 6656; i += down ? -1 : 1)
    if (initialized(p.dlmm, p.dlmm_bitmap.get(), i)) { out = i; return true; }
  return false;
}
Result<u128> price(int32_t id, uint16_t step) {
  // The official pow deliberately inverts using u128::MAX, not 2^128.
  uint32_t exp = uint32_t(std::abs(int64_t(id)));
  if (!step || exp >= 0x80000) return fail(Errc::overflow, "dlmm price exponent");
  if (!exp) return ONE;
  u128 base = ONE + (u128(step) << 64) / 10000, result = ONE;
  bool invert = id < 0;
  if (base >= result) { base = ~u128(0) / base; invert = !invert; }
  for (unsigned bit = 0; bit < 19; ++bit) {
    if (exp & (1u << bit)) {
      U256 m = U256::mul(result, base);
      if (!m.fits_u128()) return fail(Errc::overflow, "dlmm pow product");
      result = m.lo128() >> 64;
    }
    if (bit != 18) base = (base * base) >> 64;
  }
  if (!result) return fail(Errc::overflow, "dlmm price underflow");
  return invert ? ~u128(0) / result : result;
}
Result<state::DlmmPair> fee_context(const PoolState& p) {
  auto v = p.dlmm;
  if (!p.dlmm_clock_slot || p.dlmm_clock.slot != p.dlmm_clock_slot || p.dlmm_clock_slot < p.pool_slot ||
      p.dlmm_clock_slot - p.pool_slot > 32 || p.dlmm_clock.unix_timestamp < 0)
    return fail(Errc::not_quotable, "dlmm missing/stale clock");
  const int64_t now = p.dlmm_clock.unix_timestamp;
  // A backwards timestamp is conservatively refused rather than extrapolated.
  if (now < v.last_update_timestamp) return fail(Errc::not_quotable, "dlmm backwards clock");
  if (now - v.last_update_timestamp >= v.filter_period) {
    v.index_reference = v.active_id;
    v.volatility_reference = 0;
    if (now - v.last_update_timestamp < v.decay_period) {
      uint64_t reduced = uint64_t(v.volatility_accumulator) * v.reduction_factor / 10000;
      if (reduced > UINT32_MAX) return fail(Errc::overflow, "dlmm reduction overflow");
      v.volatility_reference = uint32_t(reduced);
    }
  }
  return v;
}
Result<uint64_t> fee_rate(state::DlmmPair& p, int32_t id) {
  uint64_t delta = uint64_t(std::abs(int64_t(p.index_reference) - id));
  p.volatility_accumulator = uint32_t(std::min<uint64_t>(p.max_volatility_accumulator, uint64_t(p.volatility_reference) + delta * 10000));
  if (p.base_fee_power_factor > 18) return fail(Errc::overflow, "dlmm fee power");
  u128 base = u128(p.base_factor) * p.bin_step * 10;
  for (uint8_t i = 0; i < p.base_fee_power_factor; ++i) base *= 10;
  u128 v = u128(p.volatility_accumulator) * p.bin_step;
  u128 variable = ceildiv(u128(p.variable_fee_control) * v * v, 100'000'000'000ULL);
  return uint64_t(std::min<u128>(100'000'000, base + variable));
}
u128 mid(const PoolState& p, bool down) {
  auto px = price(p.dlmm.active_id, p.dlmm.bin_step);
  if (!px || !*px) return 0;
  if (down) return *px;
  U256 q, r;
  U256::from_words(0, 1, 0, 0).divmod(U256::from_u128(*px), q, r);
  return q.fits_u128() ? q.lo128() : ~u128(0);
}
u128 spot(const PoolState& p, bool down) {
  if (!DlmmQuoter{}.supports(p)) return 0;
  auto c = fee_context(p);
  if (!c) return 0;
  int32_t idx;
  if (!next_array(p, array_index(c->active_id), down, idx)) return 0;
  auto a = array(p, idx);
  if (!a) return 0;
  int32_t id = std::clamp(c->active_id, idx * 70, idx * 70 + 69);
  for (; array_index(id) == idx; id += down ? -1 : 1) {
    const auto& b = a->bins[size_t(id - idx * 70)];
    if (!(down ? b.amount_y : b.amount_x) && !(c->supports_limit_orders() && b.limit_order_ask_side != down &&
        (b.open_order_amount || b.processed_order_remaining))) continue;
    auto f = fee_rate(*c, id);
    auto px = price(id, c->bin_step);
    if (!f || !px) return 0;
    PoolState copy; copy.dlmm.active_id = id; copy.dlmm.bin_step = c->bin_step;
    U256 q, r;
    U256::mul(mid(copy, down), DEN - *f).divmod(U256::from_u128(DEN), q, r);
    return q.fits_u128() ? q.lo128() : ~u128(0);
  }
  return 0;
}
uint64_t depth(const PoolState& p, bool down) {
  // Conservative sizing bound: input needed to exhaust the active bin's MM liquidity.
  auto a = array(p, array_index(p.dlmm.active_id));
  if (!a) return 0;
  const auto& b = a->bins[size_t(p.dlmm.active_id - a->index * 70)];
  auto px = b.price ? Result<u128>(b.price) : price(p.dlmm.active_id, p.dlmm.bin_step);
  if (!px) return 0;
  auto n = convert(down ? b.amount_y : b.amount_x, *px, !down, true);
  return n ? *n : 0;
}
} // namespace arb::dex::dlmm

namespace arb::dex {
bool DlmmQuoter::supports(const PoolState& p) const {
  return p.kind == DexKind::Dlmm && p.dlmm.token_x_program_flag == 0 && p.dlmm.token_y_program_flag == 0 &&
    p.dlmm.status == 0 && p.dlmm.pair_type <= 3 && p.dlmm.activation_type <= 1 &&
    p.dlmm.function_type <= 2 && p.dlmm.collect_fee_mode <= 1 && p.dlmm.base_fee_power_factor <= 18 &&
    (!p.mint_a_info || p.mint_a_info->token_program == prog::TOKEN) &&
    (!p.mint_b_info || p.mint_b_info->token_program == prog::TOKEN);
}
Result<Quote> DlmmQuoter::quote_exact_in(const PoolState& p, bool down, uint64_t amount) const {
  if (!p.complete || !p.dlmm_bitmap_known) return fail(Errc::not_quotable, "dlmm incomplete");
  if (!supports(p)) return fail(Errc::unsupported, "dlmm pool mode/token program");
  if (!amount) return fail(Errc::zero_amount, "dlmm zero input");
  auto c = dlmm::fee_context(p);
  if (!c) return std::unexpected(c.error());
  if ((c->pair_type == 1 || c->pair_type == 2) &&
      (c->activation_type == 0 ? p.dlmm_clock.slot : uint64_t(p.dlmm_clock.unix_timestamp)) < c->activation_point)
    return fail(Errc::not_quotable, "dlmm not activated");
  Quote q; q.est_compute_units = cu_;
  uint64_t left = amount;
  int32_t id = c->active_id;
  int arrays = 0, last_array = INT32_MIN;
  while (left) {
    int32_t idx;
    if (!dlmm::next_array(p, dlmm::array_index(id), down, idx)) return fail(Errc::insufficient_liquidity, "dlmm exhausted bitmap");
    if (idx != last_array && ++arrays > 3) { q.crosses_boundary = true; break; }
    last_array = idx;
    const auto* a = dlmm::array(p, idx);
    if (!a) { q.crosses_boundary = q.cache_gap = true; break; }
    id = std::clamp(id, idx * 70, idx * 70 + 69);
    const auto& b = a->bins[size_t(id - idx * 70)];
    uint64_t layers[3] = {down ? b.amount_y : b.amount_x, 0, 0};
    if (c->supports_limit_orders() && b.limit_order_ask_side != down) {
      layers[1] = b.processed_order_remaining; layers[2] = b.open_order_amount;
    }
    if (layers[0] || layers[1] || layers[2]) {
      auto rate = dlmm::fee_rate(*c, id);
      if (!rate) return std::unexpected(rate.error());
      auto px = b.price ? Result<u128>(b.price) : dlmm::price(id, c->bin_step);
      if (!px) return std::unexpected(px.error());
      bool input_fee = c->fee_on_input(down);
      uint64_t fee = input_fee ? uint64_t(dlmm::ceildiv(u128(left) * *rate, dlmm::DEN)) : 0;
      uint64_t net = left - fee, remain = net, out = 0;
      for (auto layer : layers) {
        if (!layer || !remain) continue;
        auto cap = dlmm::convert(layer, *px, !down, true);
        if (!cap) return std::unexpected(cap.error());
        uint64_t used = std::min(remain, *cap);
        auto got = remain >= *cap ? Result<uint64_t>(layer) : dlmm::convert(used, *px, down, false);
        if (!got || *got > UINT64_MAX - out) return fail(Errc::overflow, "dlmm output overflow");
        out += *got; remain -= used;
      }
      uint64_t used = left;
      if (remain) {
        net -= remain;
        fee = input_fee ? uint64_t(dlmm::ceildiv(u128(net) * *rate, dlmm::DEN - *rate)) : 0;
        if (fee > UINT64_MAX - net) return fail(Errc::overflow, "dlmm fee overflow");
        used = net + fee;
      }
      if (!input_fee) { fee = uint64_t(dlmm::ceildiv(u128(out) * *rate, dlmm::DEN)); out -= fee; }
      if (out > UINT64_MAX - q.amount_out || fee > UINT64_MAX - q.fee_paid || used > left)
        return fail(Errc::overflow, "dlmm totals overflow");
      q.amount_out += out; q.fee_paid += fee; left -= used;
    }
    if (left) {
      id += down ? -1 : 1;
      if (id < -443636 || id > 443636) return fail(Errc::insufficient_liquidity, "dlmm bin limit");
    }
  }
  q.amount_in = amount - left;
  if (!q.amount_out && !q.crosses_boundary) return fail(Errc::zero_amount, "dlmm zero output");
  return q;
}
} // namespace arb::dex
