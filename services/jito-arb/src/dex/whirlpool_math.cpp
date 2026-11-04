// Ported from orca-so/whirlpools@cc683ec92acb5e7606caa3d8e514d9383e9c7c11,
// programs/whirlpool/src/math/{tick_math,token_math,swap_math,bit_math}.rs.
#include "dex/whirlpool_math.hpp"

#include "core/u256.hpp"

namespace arb::dex::wp {

namespace {

constexpr u128 U(uint64_t hi, uint64_t lo) { return (u128(hi) << 64) | lo; }

// tick_math.rs: mul_shift_96(n0, n1) = (mul_u256(n0, n1) >> 96).try_into_u128().unwrap()
u128 mul_shift_96(u128 n0, u128 n1) { return U256::mul(n0, n1).shr(96).lo128(); }

u128 get_sqrt_price_positive_tick(int32_t tick) {
  u128 ratio = (tick & 1) ? U(4295182038ULL, 18379584988209926159ULL) : U(4294967296ULL, 0ULL);
  static constexpr u128 k[] = {
      U(4295396792ULL, 13458744476178488859ULL), U(4295826332ULL, 7542374851780082549ULL),
      U(4296685540ULL, 11688190168964099665ULL), U(4298404472ULL, 12325950504610477152ULL),
      U(4301844400ULL, 743749799984496066ULL),   U(4308732515ULL, 12976902672507316901ULL),
      U(4322541852ULL, 8371584169514950494ULL),  U(4350293443ULL, 2262161228410220004ULL),
      U(4406332280ULL, 14459650751607002531ULL), U(4520584868ULL, 7840076245800284593ULL),
      U(4758054286ULL, 7174944039344897962ULL),  U(5271071706ULL, 358097327435643877ULL),
      U(6469012454ULL, 11388944843577404226ULL), U(9743525213ULL, 1754717824462694519ULL),
      U(22104076011ULL, 11498634451058391528ULL), U(113758765237ULL, 5866026427582439525ULL),
      U(3013074553645ULL, 17854934707512043213ULL), U(2113780534320708ULL, 10158072769176329423ULL),
  };
  for (int i = 0; i < 18; ++i)
    if (tick & (2 << i)) ratio = mul_shift_96(ratio, k[i]);
  return ratio >> 32;
}

u128 get_sqrt_price_negative_tick(int32_t tick) {
  int32_t abs_tick = tick < 0 ? -tick : tick;
  u128 ratio = (abs_tick & 1) ? u128(18445821805675392311ULL) : (u128(1) << 64);
  static constexpr uint64_t k[] = {
      18444899583751176498ULL, 18443055278223354162ULL, 18439367220385604838ULL, 18431993317065449817ULL,
      18417254355718160513ULL, 18387811781193591352ULL, 18329067761203520168ULL, 18212142134806087854ULL,
      17980523815641551639ULL, 17526086738831147013ULL, 16651378430235024244ULL, 15030750278693429944ULL,
      12247334978882834399ULL, 8131365268884726200ULL,  3584323654723342297ULL,  696457651847595233ULL,
      26294789957452057ULL,    37481735321082ULL,
  };
  for (int i = 0; i < 18; ++i)
    if (abs_tick & (2 << i)) ratio = (ratio * k[i]) >> 64;
  return ratio;
}

int clz128(u128 v) {
  uint64_t hi = uint64_t(v >> 64);
  if (hi) return __builtin_clzll(hi);
  uint64_t lo = uint64_t(v);
  return lo ? 64 + __builtin_clzll(lo) : 128;
}

// bit_math.rs checked_mul_div_round_up_if
Result<u128> checked_mul_div_round_up_if(u128 n0, u128 n1, u128 d, bool round_up) {
  if (d == 0) return fail(Errc::div_by_zero, "DivideByZero");
  if (n0 != 0 && n1 > ~u128(0) / n0) return fail(Errc::overflow, "MulDivOverflow");
  u128 p = n0 * n1;
  u128 n = p / d;
  return (round_up && p % d > 0) ? n + 1 : n;
}

// bit_math.rs div_round_up_if
Result<u128> div_round_up_if(u128 n, u128 d, bool round_up) {
  if (d == 0) return fail(Errc::div_by_zero, "DivideByZero");
  u128 q = n / d;
  return (round_up && n % d > 0) ? q + 1 : q;
}

// bit_math.rs div_round_up_if_u256
Result<u128> div_round_up_if_u256(const U256& n, const U256& d, bool round_up) {
  U256 q, r;
  n.divmod(d, q, r);
  U256 result = (round_up && !r.is_zero()) ? q.add(U256::from_u128(1)) : q;
  if (!result.fits_u128()) return fail(Errc::overflow, "NumberDownCastError");
  return result.lo128();
}

// token_math.rs get_next_sqrt_price_from_a_round_up
Result<u128> next_sqrt_price_from_a_round_up(u128 sqrt_price, u128 liquidity, uint64_t amount, bool input) {
  if (amount == 0) return sqrt_price;
  U256 product = U256::mul(sqrt_price, u128(amount));
  U256 numerator;
  if (!U256::mul(liquidity, sqrt_price).checked_shl64(numerator)) return fail(Errc::overflow, "MultiplicationOverflow");
  U256 liquidity_shift_left = U256::from_u128(liquidity).shl64();
  if (!input && liquidity_shift_left.lte(product)) return fail(Errc::div_by_zero, "DivideByZero");
  U256 denominator = input ? liquidity_shift_left.add(product) : liquidity_shift_left.sub(product);
  auto price = div_round_up_if_u256(numerator, denominator, true);
  if (!price) return price;
  if (*price < MIN_SQRT_PRICE_X64) return fail(Errc::overflow, "TokenMinSubceeded");
  if (*price > MAX_SQRT_PRICE_X64) return fail(Errc::overflow, "TokenMaxExceeded");
  return price;
}

// token_math.rs get_next_sqrt_price_from_b_round_down
Result<u128> next_sqrt_price_from_b_round_down(u128 sqrt_price, u128 liquidity, uint64_t amount, bool input) {
  u128 amount_x64 = u128(amount) << 64;
  auto delta = div_round_up_if(amount_x64, liquidity, !input);
  if (!delta) return delta;
  if (input) {
    if (sqrt_price > ~u128(0) - *delta) return fail(Errc::overflow, "SqrtPriceOutOfBounds");
    return sqrt_price + *delta;
  }
  if (sqrt_price < *delta) return fail(Errc::overflow, "SqrtPriceOutOfBounds");
  return sqrt_price - *delta;
}

Result<Delta> try_fixed_delta(u128 cur, u128 target, u128 l, bool input, bool a_to_b) {
  return a_to_b == input ? try_get_amount_delta_a(cur, target, l, input) : try_get_amount_delta_b(cur, target, l, input);
}
Result<uint64_t> fixed_delta(u128 cur, u128 target, u128 l, bool input, bool a_to_b) {
  return a_to_b == input ? get_amount_delta_a(cur, target, l, input) : get_amount_delta_b(cur, target, l, input);
}
Result<uint64_t> unfixed_delta(u128 cur, u128 target, u128 l, bool input, bool a_to_b) {
  return a_to_b == input ? get_amount_delta_b(cur, target, l, !input) : get_amount_delta_a(cur, target, l, !input);
}

}  // namespace

u128 sqrt_price_from_tick_index(int32_t tick) {
  return tick >= 0 ? get_sqrt_price_positive_tick(tick) : get_sqrt_price_negative_tick(tick);
}

int32_t tick_index_from_sqrt_price(u128 sqrt_price_x64) {
  constexpr i128 LOG_B_2_X32 = 59543866431248;
  constexpr uint32_t BIT_PRECISION = 14;
  constexpr i128 LOG_B_P_ERR_MARGIN_LOWER_X64 = 184467440737095516;
  constexpr i128 LOG_B_P_ERR_MARGIN_UPPER_X64 = i128(15793534762490258745ULL);
  uint32_t msb = uint32_t(128 - clz128(sqrt_price_x64) - 1);
  i128 log2p_integer_x32 = (i128(msb) - 64) * (i128(1) << 32);  // (msb - 64) << 32
  i128 bit = i128(0x8000000000000000ULL);
  uint32_t precision = 0;
  i128 log2p_fraction_x64 = 0;
  u128 r = msb >= 64 ? sqrt_price_x64 >> (msb - 63) : sqrt_price_x64 << (63 - msb);
  while (bit > 0 && precision < BIT_PRECISION) {
    r *= r;
    u128 is_r_more_than_two = r >> 127;
    r >>= 63 + uint32_t(is_r_more_than_two);
    log2p_fraction_x64 += bit * i128(is_r_more_than_two);
    bit >>= 1;
    precision += 1;
  }
  i128 log2p_fraction_x32 = log2p_fraction_x64 >> 32;
  i128 log2p_x32 = log2p_integer_x32 + log2p_fraction_x32;
  i128 logbp_x64 = log2p_x32 * LOG_B_2_X32;
  int32_t tick_low = int32_t((logbp_x64 - LOG_B_P_ERR_MARGIN_LOWER_X64) >> 64);
  int32_t tick_high = int32_t((logbp_x64 + LOG_B_P_ERR_MARGIN_UPPER_X64) >> 64);
  if (tick_low == tick_high) return tick_low;
  return sqrt_price_from_tick_index(tick_high) <= sqrt_price_x64 ? tick_high : tick_low;
}

Result<Delta> try_get_amount_delta_a(u128 sqrt_price_0, u128 sqrt_price_1, u128 liquidity, bool round_up) {
  u128 lower = sqrt_price_0 > sqrt_price_1 ? sqrt_price_1 : sqrt_price_0;
  u128 upper = sqrt_price_0 > sqrt_price_1 ? sqrt_price_0 : sqrt_price_1;
  u128 diff = upper - lower;
  U256 numerator;
  if (!U256::mul(liquidity, diff).checked_shl64(numerator)) return fail(Errc::overflow, "MultiplicationOverflow");
  U256 denominator = U256::mul(upper, lower);
  U256 q, r;
  numerator.divmod(denominator, q, r);
  U256 result = (round_up && !r.is_zero()) ? q.add(U256::from_u128(1)) : q;
  if (!result.fits_u128()) return Delta{0, true};  // try_into_u128 Err -> ExceedsMax
  u128 v = result.lo128();
  if (v > u128(UINT64_MAX)) return Delta{0, true};
  return Delta{uint64_t(v), false};
}

Result<Delta> try_get_amount_delta_b(u128 sqrt_price_0, u128 sqrt_price_1, u128 liquidity, bool round_up) {
  u128 lower = sqrt_price_0 > sqrt_price_1 ? sqrt_price_1 : sqrt_price_0;
  u128 upper = sqrt_price_0 > sqrt_price_1 ? sqrt_price_0 : sqrt_price_1;
  u128 n0 = liquidity, n1 = upper - lower;
  if (n0 == 0 || n1 == 0) return Delta{0, false};
  if (n1 > ~u128(0) / n0) return Delta{0, true};  // checked_mul None -> ExceedsMax
  u128 p = n0 * n1;
  uint64_t result = uint64_t(p >> 64);
  bool should_round = round_up && (p & Q64_MASK) > 0;
  if (should_round && result == UINT64_MAX) return Delta{0, true};
  return Delta{should_round ? result + 1 : result, false};
}

Result<uint64_t> get_amount_delta_a(u128 s0, u128 s1, u128 l, bool round_up) {
  auto d = try_get_amount_delta_a(s0, s1, l, round_up);
  if (!d) return std::unexpected(d.error());
  if (d->exceeds) return fail(Errc::overflow, "TokenMaxExceeded");
  return d->value;
}

Result<uint64_t> get_amount_delta_b(u128 s0, u128 s1, u128 l, bool round_up) {
  auto d = try_get_amount_delta_b(s0, s1, l, round_up);
  if (!d) return std::unexpected(d.error());
  if (d->exceeds) return fail(Errc::overflow, "MultiplicationShiftRightOverflow");
  return d->value;
}

Result<u128> get_next_sqrt_price(u128 sqrt_price, u128 liquidity, uint64_t amount, bool input, bool a_to_b) {
  if (input == a_to_b) return next_sqrt_price_from_a_round_up(sqrt_price, liquidity, amount, input);
  return next_sqrt_price_from_b_round_down(sqrt_price, liquidity, amount, input);
}

Result<SwapStep> compute_swap(uint64_t amount_remaining, uint32_t fee_rate, u128 liquidity, u128 sqrt_price_current,
                              u128 sqrt_price_target, bool input, bool a_to_b) {
  auto initial = try_fixed_delta(sqrt_price_current, sqrt_price_target, liquidity, input, a_to_b);
  if (!initial) return std::unexpected(initial.error());
  uint64_t amount_calc = amount_remaining;
  if (input) {
    auto c = checked_mul_div_round_up_if(u128(amount_remaining), FEE_RATE_MUL_VALUE - fee_rate, FEE_RATE_MUL_VALUE, false);
    if (!c) return std::unexpected(c.error());
    if (*c > u128(UINT64_MAX)) return fail(Errc::overflow, "NumberDownCastError");
    amount_calc = uint64_t(*c);
  }
  bool lte = !initial->exceeds && initial->value <= amount_calc;
  u128 next_sqrt_price;
  if (lte) {
    next_sqrt_price = sqrt_price_target;
  } else {
    auto n = get_next_sqrt_price(sqrt_price_current, liquidity, amount_calc, input, a_to_b);
    if (!n) return std::unexpected(n.error());
    next_sqrt_price = *n;
  }
  bool is_max_swap = next_sqrt_price == sqrt_price_target;
  auto unfixed = unfixed_delta(sqrt_price_current, next_sqrt_price, liquidity, input, a_to_b);
  if (!unfixed) return std::unexpected(unfixed.error());
  uint64_t amount_fixed;
  if (!is_max_swap || initial->exceeds) {
    auto f = fixed_delta(sqrt_price_current, next_sqrt_price, liquidity, input, a_to_b);
    if (!f) return std::unexpected(f.error());
    amount_fixed = *f;
  } else {
    amount_fixed = initial->value;
  }
  SwapStep s;
  s.amount_in = input ? amount_fixed : *unfixed;
  s.amount_out = input ? *unfixed : amount_fixed;
  if (!input && s.amount_out > amount_remaining) s.amount_out = amount_remaining;
  if (input && !is_max_swap) {
    if (amount_remaining < s.amount_in) return fail(Errc::overflow, "fee underflow");  // a Rust panic on chain
    s.fee_amount = amount_remaining - s.amount_in;
  } else {
    auto f = checked_mul_div_round_up_if(u128(s.amount_in), fee_rate, FEE_RATE_MUL_VALUE - fee_rate, true);
    if (!f) return std::unexpected(f.error());
    if (*f > u128(UINT64_MAX)) return fail(Errc::overflow, "NumberDownCastError");
    s.fee_amount = uint64_t(*f);
  }
  s.next_price = next_sqrt_price;
  return s;
}

}  // namespace arb::dex::wp
