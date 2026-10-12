#pragma once
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>

#include "core/types.hpp"

namespace pd {

constexpr int8_t b58_index(char c) {
  constexpr std::string_view alpha = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
  for (size_t i = 0; i < alpha.size(); ++i)
    if (alpha[i] == c) return static_cast<int8_t>(i);
  return -1;
}

/** Decode base58 into exactly N bytes (big-endian value). False on bad char or overflow. */
template <size_t N>
constexpr bool b58_decode_fixed(std::string_view s, std::array<uint8_t, N>& out) {
  for (auto& b : out) b = 0;
  if (s.empty() || s.size() > N * 2) return false;
  for (char c : s) {
    int idx = b58_index(c);
    if (idx < 0) return false;
    uint32_t carry = static_cast<uint32_t>(idx);
    for (size_t i = N; i-- > 0;) {
      carry += 58u * out[i];
      out[i] = static_cast<uint8_t>(carry & 0xff);
      carry >>= 8;
    }
    if (carry) return false;
  }
  // Leading '1's must map to leading zero bytes and vice versa (canonical form).
  size_t ones = 0;
  while (ones < s.size() && s[ones] == '1') ++ones;
  size_t zeros = 0;
  while (zeros < N && out[zeros] == 0) ++zeros;
  return ones == zeros;
}

/** Compile-time pubkey literal; a typo is a compile error. */
consteval Pubkey PK(std::string_view s) {
  Pubkey k{};
  if (!b58_decode_fixed(s, k)) throw "invalid base58 pubkey literal";
  return k;
}

inline bool b58_decode32(std::string_view s, Pubkey& out) { return b58_decode_fixed(s, out); }

/** Encode into `out` (capacity >= 2*n). Returns length. No allocation. */
size_t b58_encode(const uint8_t* data, size_t n, char* out);

inline std::string b58(const uint8_t* data, size_t n) {
  char buf[128];
  size_t len = b58_encode(data, n, buf);
  return std::string(buf, len);
}
inline std::string b58(const Pubkey& k) { return b58(k.data(), 32); }
inline std::string b58(const Signature& s) { return b58(s.data(), 64); }

inline Pubkey parse_pubkey(std::string_view s) {
  Pubkey k;
  if (!b58_decode32(s, k)) throw std::runtime_error("invalid pubkey: " + std::string(s));
  return k;
}

}  // namespace pd
