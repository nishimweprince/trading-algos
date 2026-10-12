#pragma once
// Fixed-size Solana primitives. Everything on the hot path is a value type:
// no std::string, no heap.
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace pd {

using Pubkey = std::array<uint8_t, 32>;
using Hash = std::array<uint8_t, 32>;
using Signature = std::array<uint8_t, 64>;

inline bool is_zero(const Pubkey& k) {
  uint64_t acc = 0;
  for (size_t i = 0; i < 32; i += 8) {
    uint64_t w;
    std::memcpy(&w, k.data() + i, 8);
    acc |= w;
  }
  return acc == 0;
}

inline Pubkey key_from(const uint8_t* p) {
  Pubkey k;
  std::memcpy(k.data(), p, 32);
  return k;
}

inline bool key_eq(const uint8_t* p, const Pubkey& k) { return std::memcmp(p, k.data(), 32) == 0; }

/** Pubkeys are already uniformly distributed: the first 8 bytes are a fine hash. */
inline uint64_t key_hash(const Pubkey& k) {
  uint64_t h;
  std::memcpy(&h, k.data(), 8);
  return h ^ (h >> 29);
}

constexpr uint64_t LAMPORTS_PER_SOL = 1'000'000'000ULL;
constexpr int PUMP_DECIMALS = 6;

}  // namespace pd
