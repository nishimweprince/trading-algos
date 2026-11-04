#pragma once
// 256-bit unsigned integer with the operations Whirlpool's U256Muldiv uses
// (programs/whirlpool/src/math/u256_math.rs): 128x128 multiply, 64-bit word
// shifts, add/sub, compare and exact division with remainder. Results are
// exact, so any correct division reproduces the program's floor/remainder.
#include <cstdint>

#include "core/bytes.hpp"

namespace arb {

struct U256 {
  uint64_t w[4]{};  // little-endian 64-bit limbs

  static constexpr U256 from_u128(u128 v) {
    U256 r;
    r.w[0] = static_cast<uint64_t>(v);
    r.w[1] = static_cast<uint64_t>(v >> 64);
    return r;
  }
  static constexpr U256 from_words(uint64_t w3, uint64_t w2, uint64_t w1, uint64_t w0) {
    U256 r;
    r.w[0] = w0; r.w[1] = w1; r.w[2] = w2; r.w[3] = w3;
    return r;
  }

  /** Full 128x128 -> 256 product (mul_u256). */
  static U256 mul(u128 a, u128 b) {
    uint64_t a0 = uint64_t(a), a1 = uint64_t(a >> 64), b0 = uint64_t(b), b1 = uint64_t(b >> 64);
    u128 p00 = u128(a0) * b0, p01 = u128(a0) * b1, p10 = u128(a1) * b0, p11 = u128(a1) * b1;
    U256 r;
    r.w[0] = uint64_t(p00);
    u128 mid = (p00 >> 64) + uint64_t(p01) + uint64_t(p10);
    r.w[1] = uint64_t(mid);
    u128 hi = (mid >> 64) + (p01 >> 64) + (p10 >> 64) + uint64_t(p11);
    r.w[2] = uint64_t(hi);
    r.w[3] = uint64_t((hi >> 64) + (p11 >> 64));
    return r;
  }

  constexpr bool is_zero() const { return (w[0] | w[1] | w[2] | w[3]) == 0; }
  constexpr bool fits_u128() const { return (w[2] | w[3]) == 0; }
  constexpr u128 lo128() const { return (u128(w[1]) << 64) | w[0]; }

  /** shift_word_left: << 64, dropping the top word. */
  constexpr U256 shl64() const { return from_words(w[2], w[1], w[0], 0); }
  /** checked_shift_word_left: fails when the top word is non-zero. */
  constexpr bool checked_shl64(U256& out) const {
    if (w[3] != 0) return false;
    out = shl64();
    return true;
  }
  /** Logical right shift by n bits (n < 256). */
  constexpr U256 shr(unsigned n) const {
    U256 r;
    if (n >= 256) return r;
    unsigned words = n / 64, bits = n % 64;
    for (unsigned i = 0; i + words < 4; ++i) {
      uint64_t lo = w[i + words];
      uint64_t hi = (i + words + 1 < 4) ? w[i + words + 1] : 0;
      r.w[i] = bits ? (lo >> bits) | (hi << (64 - bits)) : lo;
    }
    return r;
  }
  /** Wrapping add/sub (the program's add/sub wrap the same way). */
  constexpr U256 add(const U256& o) const {
    U256 r;
    u128 c = 0;
    for (int i = 0; i < 4; ++i) {
      c += u128(w[i]) + o.w[i];
      r.w[i] = uint64_t(c);
      c >>= 64;
    }
    return r;
  }
  constexpr U256 sub(const U256& o) const {
    U256 r;
    uint64_t borrow = 0;
    for (int i = 0; i < 4; ++i) {
      u128 d = u128(w[i]) - o.w[i] - borrow;
      r.w[i] = uint64_t(d);
      borrow = (d >> 64) ? 1 : 0;
    }
    return r;
  }
  constexpr int cmp(const U256& o) const {
    for (int i = 3; i >= 0; --i) {
      if (w[i] != o.w[i]) return w[i] < o.w[i] ? -1 : 1;
    }
    return 0;
  }
  constexpr bool lte(const U256& o) const { return cmp(o) <= 0; }
  constexpr bool operator==(const U256& o) const { return cmp(o) == 0; }

  /** Exact quotient and remainder. d must be non-zero (callers check). */
  void divmod(const U256& d, U256& q, U256& r) const;
};

}  // namespace arb
