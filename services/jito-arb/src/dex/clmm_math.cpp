#include "dex/clmm_math.hpp"

#include <array>
#include <cstdlib>

#include "core/u256.hpp"

namespace arb::dex::clmm {

namespace {

// ---- 512-bit unsigned integers, just enough for exact mul-div ----

struct Big {
  std::array<uint64_t, 8> w{};  // little-endian limbs
  static Big of(u128 v) {
    Big b;
    b.w[0] = uint64_t(v);
    b.w[1] = uint64_t(v >> 64);
    return b;
  }
  bool zero() const {
    for (uint64_t x : w)
      if (x) return false;
    return true;
  }
  int cmp(const Big& o) const {
    for (int i = 7; i >= 0; --i)
      if (w[size_t(i)] != o.w[size_t(i)]) return w[size_t(i)] < o.w[size_t(i)] ? -1 : 1;
    return 0;
  }
  bool fits_u64() const {
    for (size_t i = 1; i < 8; ++i)
      if (w[i]) return false;
    return true;
  }
  bool fits_u128() const {
    for (size_t i = 2; i < 8; ++i)
      if (w[i]) return false;
    return true;
  }
  u128 lo128() const { return u128(w[0]) | (u128(w[1]) << 64); }
  int bits() const {
    for (int i = 7; i >= 0; --i)
      if (w[size_t(i)]) return i * 64 + 64 - __builtin_clzll(w[size_t(i)]);
    return 0;
  }
  bool bit(int n) const { return (w[size_t(n / 64)] >> (n % 64)) & 1; }
};

Big add(const Big& a, const Big& b) {
  Big r;
  u128 carry = 0;
  for (size_t i = 0; i < 8; ++i) {
    u128 s = u128(a.w[i]) + b.w[i] + carry;
    r.w[i] = uint64_t(s);
    carry = s >> 64;
  }
  return r;
}

Big sub(const Big& a, const Big& b) {  // a >= b
  Big r;
  uint64_t borrow = 0;
  for (size_t i = 0; i < 8; ++i) {
    u128 d = u128(a.w[i]) - b.w[i] - borrow;
    r.w[i] = uint64_t(d);
    borrow = (d >> 64) ? 1 : 0;
  }
  return r;
}

Big mul(const Big& a, const Big& b) {  // the product must fit 512 bits
  Big r;
  for (size_t i = 0; i < 8; ++i) {
    if (!a.w[i]) continue;
    u128 carry = 0;
    for (size_t j = 0; i + j < 8; ++j) {
      u128 cur = u128(a.w[i]) * b.w[j] + r.w[i + j] + carry;
      r.w[i + j] = uint64_t(cur);
      carry = cur >> 64;
    }
  }
  return r;
}

Big shl(const Big& a, int n) {
  Big r;
  int limbs = n / 64, bits = n % 64;
  for (int i = 7; i >= 0; --i) {
    int src = i - limbs;
    if (src < 0) continue;
    uint64_t v = a.w[size_t(src)] << bits;
    if (bits && src > 0) v |= a.w[size_t(src - 1)] >> (64 - bits);
    r.w[size_t(i)] = v;
  }
  return r;
}

Big shr(const Big& a, int n) {
  Big r;
  int limbs = n / 64, bits = n % 64;
  for (int i = 0; i < 8; ++i) {
    int src = i + limbs;
    if (src > 7) continue;
    uint64_t v = a.w[size_t(src)] >> bits;
    if (bits && src < 7) v |= a.w[size_t(src + 1)] << (64 - bits);
    r.w[size_t(i)] = v;
  }
  return r;
}

void divmod(const Big& n, const Big& d, Big& q, Big& r) {
  q = Big{};
  r = Big{};
  for (int i = n.bits() - 1; i >= 0; --i) {
    r = shl(r, 1);
    if (n.bit(i)) r.w[0] |= 1;
    if (r.cmp(d) >= 0) {
      r = sub(r, d);
      q.w[size_t(i / 64)] |= uint64_t(1) << (i % 64);
    }
  }
}

/** floor or ceil of a * b / d (d > 0). */
Big mul_div(const Big& a, const Big& b, const Big& d, bool round_up) {
  Big q, r;
  divmod(mul(a, b), d, q, r);
  if (round_up && !r.zero()) q = add(q, Big::of(1));
  return q;
}

u128 mul_shift64(u128 ratio, uint64_t m) {  // (ratio * m) >> 64 with ratio <= 2^128
  U256 p = U256::mul(ratio, u128(m));
  return p.shr(64).lo128();
}

}  // namespace

// ---- tick math ----

Result<u128> sqrt_price_at_tick(int32_t tick) {
  if (tick < MIN_TICK || tick > MAX_TICK) return fail(Errc::overflow, "TickUpperOverflow");
  uint32_t a = uint32_t(std::abs(tick));
  static const uint64_t M[19] = {0xfffcb933bd6fb800, 0xfff97272373d4000, 0xfff2e50f5f657000, 0xffe5caca7e10f000,
                                 0xffcb9843d60f7000, 0xff973b41fa98e800, 0xff2ea16466c9b000, 0xfe5dee046a9a3800,
                                 0xfcbe86c7900bb000, 0xf987a7253ac65800, 0xf3392b0822bb6000, 0xe7159475a2caf000,
                                 0xd097f3bdfd2f2000, 0xa9f746462d9f8000, 0x70d869a156f31c00, 0x31be135f97ed3200,
                                 0x9aa508b5b85a500,  0x5d6af8dedc582c,   0x2216e584f5fa};
  u128 ratio = (a & 1) ? u128(M[0]) : (u128(1) << 64);
  for (int i = 1; i < 19; ++i)
    if (a & (1u << i)) ratio = mul_shift64(ratio, M[i]);
  if (tick > 0) ratio = ~u128(0) / ratio;
  return ratio;
}

Result<int32_t> tick_at_sqrt_price(u128 sqrt) {
  if (sqrt < MIN_SQRT_PRICE_X64 || sqrt >= MAX_SQRT_PRICE_X64) return fail(Errc::overflow, "SqrtPriceX64");
  int msb = 127 - (int(sqrt >> 64) ? __builtin_clzll(uint64_t(sqrt >> 64)) : 64 + __builtin_clzll(uint64_t(sqrt)));
  i128 log2p_integer_x32 = i128(msb - 64) * (i128(1) << 32);
  i128 bit = i128(1) << 63;
  int precision = 0;
  i128 frac_x64 = 0;
  u128 r = msb >= 64 ? sqrt >> (msb - 63) : sqrt << (63 - msb);
  while (bit > 0 && precision < 16) {
    r *= r;
    u128 more = r >> 127;
    r >>= (63 + int(more));
    frac_x64 += bit * i128(more);
    bit >>= 1;
    ++precision;
  }
  i128 log2p_x32 = log2p_integer_x32 + (frac_x64 >> 32);
  i128 log_sqrt_10001_x64 = log2p_x32 * i128(59543866431248LL);
  i128 c_low = i128(184467440737095516LL);
  i128 c_high = i128(0xdb2de8305203e539ULL);  // 15793534762490258745
  int32_t tick_low = int32_t((log_sqrt_10001_x64 - c_low) >> 64);
  int32_t tick_high = int32_t((log_sqrt_10001_x64 + c_high) >> 64);
  if (tick_low == tick_high) return tick_low;
  auto hp = sqrt_price_at_tick(tick_high);
  if (!hp) return fail(hp.error().code, hp.error().what);
  return *hp <= sqrt ? tick_high : tick_low;
}

// ---- tick arrays and the bitmap ----

int32_t tick_count(uint16_t tick_spacing) { return TICK_ARRAY_SIZE * int32_t(tick_spacing); }

int32_t array_start_index(int32_t tick, uint16_t tick_spacing) {
  int32_t n = tick_count(tick_spacing);
  int32_t start = tick / n;
  if (tick < 0 && tick % n != 0) --start;
  return start * n;
}

int32_t max_tick_in_bitmap(uint16_t tick_spacing) { return int32_t(tick_spacing) * TICK_ARRAY_SIZE * TICK_ARRAY_BITMAP_SIZE; }

void bitmap_start_range(uint16_t tick_spacing, int32_t& min_start, int32_t& max_start) {
  int32_t max_b = max_tick_in_bitmap(tick_spacing), min_b = -max_b;
  if (max_b > MAX_TICK) max_b = array_start_index(MAX_TICK, tick_spacing) + tick_count(tick_spacing);
  if (min_b < MIN_TICK) min_b = array_start_index(MIN_TICK, tick_spacing);
  min_start = min_b;
  max_start = max_b;
}

namespace {
int32_t compressed_of(int32_t start, uint16_t tick_spacing) {
  int32_t mult = int32_t(tick_spacing) * TICK_ARRAY_SIZE;
  int32_t c = start / mult + 512;
  if (start < 0 && start % mult != 0) --c;
  return c;
}
bool bit_set(const uint64_t bitmap[16], int32_t pos) { return (bitmap[pos / 64] >> (pos % 64)) & 1; }
}  // namespace

bool current_array_initialized(const uint64_t bitmap[16], int32_t tick_current, uint16_t tick_spacing, int32_t& start) {
  int32_t mult = int32_t(tick_spacing) * TICK_ARRAY_SIZE;
  int32_t c = compressed_of(tick_current, tick_spacing);
  start = (c - 512) * mult;
  int32_t pos = c;
  return pos >= 0 && pos < 1024 && bit_set(bitmap, pos);
}

bool next_initialized_array(const uint64_t bitmap[16], int32_t last_start, uint16_t tick_spacing, bool zero_for_one, int32_t& out) {
  int32_t boundary = max_tick_in_bitmap(tick_spacing);
  int32_t next = zero_for_one ? last_start - tick_count(tick_spacing) : last_start + tick_count(tick_spacing);
  if (next < -boundary || next >= boundary) {
    out = last_start;
    return false;
  }
  int32_t mult = int32_t(tick_spacing) * TICK_ARRAY_SIZE;
  int32_t pos = std::abs(compressed_of(next, tick_spacing));
  if (zero_for_one) {
    for (int32_t i = pos; i >= 0; --i)
      if (bit_set(bitmap, i)) {
        out = (i - 512) * mult;
        return true;
      }
    out = -boundary;
    return false;
  }
  for (int32_t i = pos; i < 1024; ++i)
    if (bit_set(bitmap, i)) {
      out = (i - 512) * mult;
      return true;
    }
  out = boundary - tick_count(tick_spacing);
  return false;
}

Result<int32_t> initialized_array(const state::ClmmPool& pool, const state::ClmmBitmapExtension* extension,
                                  int32_t last_start, bool zero_for_one, bool include_current) {
  if (!pool.tick_spacing) return fail(Errc::bad_data, "zero tick spacing");
  const int32_t width = tick_count(pool.tick_spacing);
  const int32_t lo = array_start_index(MIN_TICK, pool.tick_spacing);
  const int32_t hi = array_start_index(MAX_TICK, pool.tick_spacing);
  if (last_start % width || last_start < lo || last_start > hi)
    return fail(Errc::bad_data, "invalid array start");
  const int32_t step = zero_for_one ? -width : width;
  for (int32_t start = last_start + (include_current ? 0 : step); start >= lo && start <= hi;) {
    const int32_t index = start / width;
    const uint64_t* words;
    int32_t bit;
    if (index >= -512 && index < 512) {
      words = pool.tick_array_bitmap;
      bit = index + 512;
    } else {
      if (!extension) return fail(Errc::not_quotable, "missing bitmap extension");
      // Negative blocks cover [-1024,-513], [-1536,-1025], ...;
      // within each block the bit increases with the array start.
      const int32_t block = index >= 512 ? index / 512 - 1 : (-index - 1) / 512 - 1;
      if (block < 0 || block >= 14) return fail(Errc::bad_data, "bitmap extension range");
      words = index >= 512 ? extension->positive[block] : extension->negative[block];
      bit = (index % 512 + 512) % 512;
    }
    const int offset = bit % 64;
    const uint64_t mask = zero_for_one ? (~uint64_t(0) >> (63 - offset)) : (~uint64_t(0) << offset);
    const uint64_t word = words[bit / 64] & mask;
    if (word) {
      const int found = zero_for_one ? 63 - __builtin_clzll(word) : __builtin_ctzll(word);
      const int32_t candidate = start + (found - offset) * width;
      if (candidate >= lo && candidate <= hi) return candidate;
      break;
    }
    start += (zero_for_one ? -offset - 1 : 64 - offset) * width;
  }
  return fail(Errc::insufficient_liquidity, "no initialized array in direction");
}

// ---- amounts and prices ----

namespace {

/** get_delta_amounts_for_swap: (amount_in, amount_out) between two prices; nullopt = MaxTokenOverflow. */
Result<std::pair<uint64_t, uint64_t>> delta_amounts(u128 a, u128 b, u128 liquidity, bool zero_for_one, bool& max_token_overflow) {
  max_token_overflow = false;
  if (a > b) std::swap(a, b);
  if (a == 0) return fail(Errc::div_by_zero, "ZeroSqrtPrice");
  Big amount_1_x64 = mul(Big::of(liquidity), Big::of(b - a));
  Big product = mul(Big::of(a), Big::of(b));
  Big in, out;
  if (zero_for_one) {
    in = mul_div(amount_1_x64, shl(Big::of(1), 64), product, true);
    out = shr(amount_1_x64, 64);
  } else {
    in = shr(add(amount_1_x64, Big::of(UINT64_MAX)), 64);
    out = mul_div(amount_1_x64, shl(Big::of(1), 64), product, false);
  }
  if (!in.fits_u64() || !out.fits_u64()) {
    max_token_overflow = true;
    return fail(Errc::overflow, "MaxTokenOverflow");
  }
  return std::pair<uint64_t, uint64_t>{in.w[0], out.w[0]};
}

Result<u128> next_sqrt_from_input(u128 sqrt, u128 liquidity, uint64_t amount_in, bool zero_for_one) {
  if (sqrt < MIN_SQRT_PRICE_X64 || sqrt >= MAX_SQRT_PRICE_X64) return fail(Errc::overflow, "SqrtPriceX64");
  if (liquidity == 0) return fail(Errc::insufficient_liquidity, "ZeroLiquidity");
  if (amount_in == 0) return sqrt;
  if (zero_for_one) {
    // amount 0, rounding up: ceil(L << 64 * sqrt / (L << 64 + amount * sqrt))
    Big n1 = shl(Big::of(liquidity), 64);
    Big den = add(n1, mul(Big::of(amount_in), Big::of(sqrt)));
    Big r = mul_div(n1, Big::of(sqrt), den, true);
    return r.lo128();
  }
  // amount 1, rounding down: sqrt + (amount << 64) / L
  u128 q = (u128(amount_in) << 64) / liquidity;
  if (sqrt > ~u128(0) - q) return fail(Errc::overflow, "CalculateOverflow");
  return sqrt + q;
}

uint64_t mul_div_u64(uint64_t a, uint64_t num, uint64_t den, bool round_up) {
  u128 p = u128(a) * num;
  u128 q = p / den;
  if (round_up && p % den) ++q;
  return uint64_t(q);
}

}  // namespace

Result<StepResult> compute_swap_exact_in(u128 sqrt_current, u128 sqrt_target, u128 liquidity, uint64_t amount_remaining,
                                         uint32_t fee_rate, bool zero_for_one, bool fee_on_input) {
  StepResult r;
  const uint64_t D = FEE_RATE_DENOMINATOR;
  uint64_t calc = fee_on_input ? mul_div_u64(amount_remaining, D - fee_rate, D, false) : amount_remaining;
  bool overflow = false;
  auto at_target = delta_amounts(sqrt_target, sqrt_current, liquidity, zero_for_one, overflow);
  if (!at_target && !overflow) return fail(at_target.error().code, at_target.error().what);
  if (at_target && calc >= at_target->first) {
    r.sqrt_price_next_x64 = sqrt_target;
    r.amount_in = at_target->first;
    r.amount_out = at_target->second;
  } else {
    auto next = next_sqrt_from_input(sqrt_current, liquidity, calc, zero_for_one);
    if (!next) return fail(next.error().code, next.error().what);
    r.sqrt_price_next_x64 = *next;
    auto d = delta_amounts(*next, sqrt_current, liquidity, zero_for_one, overflow);
    if (!d) return fail(d.error().code, d.error().what);
    r.amount_in = d->first;
    r.amount_out = d->second;
  }
  if (zero_for_one ? r.sqrt_price_next_x64 < sqrt_target : sqrt_target < r.sqrt_price_next_x64)
    return fail(Errc::overflow, "price past target");
  if (fee_on_input) {
    if (r.sqrt_price_next_x64 != sqrt_target) {
      if (amount_remaining < r.amount_in) return fail(Errc::overflow, "CalculateOverflow");
      r.fee_amount = amount_remaining - r.amount_in;
    } else {
      r.fee_amount = mul_div_u64(r.amount_in, fee_rate, D - fee_rate, true);
    }
  } else {
    r.fee_amount = mul_div_u64(r.amount_out, fee_rate, D, true);
    r.amount_out -= r.fee_amount;
    if (r.sqrt_price_next_x64 != sqrt_target) r.amount_in = amount_remaining;
  }
  return r;
}

Result<u128> add_delta(u128 x, i128 y) {
  if (y < 0) {
    u128 m = u128(-y);
    if (m > x) return fail(Errc::overflow, "LiquiditySubValueErr");
    u128 z = x - m;
    if (!(x > z)) return fail(Errc::overflow, "LiquiditySubValueErr");
    return z;
  }
  u128 z = x + u128(y);
  if (z < x) return fail(Errc::overflow, "LiquidityAddValueErr");
  return z;
}

}  // namespace arb::dex::clmm
