#pragma once
// Little-endian readers/writers over raw account bytes.
#include <cstdint>
#include <cstring>
#include <string_view>

namespace arb {

using u128 = unsigned __int128;
using i128 = __int128;

inline uint16_t rd_u16(const uint8_t* p) { uint16_t v; std::memcpy(&v, p, 2); return v; }
inline uint32_t rd_u32(const uint8_t* p) { uint32_t v; std::memcpy(&v, p, 4); return v; }
inline uint64_t rd_u64(const uint8_t* p) { uint64_t v; std::memcpy(&v, p, 8); return v; }
inline u128 rd_u128(const uint8_t* p) { u128 v; std::memcpy(&v, p, 16); return v; }
inline i128 rd_i128(const uint8_t* p) { i128 v; std::memcpy(&v, p, 16); return v; }

inline void wr_u16(uint8_t* p, uint16_t v) { std::memcpy(p, &v, 2); }
inline void wr_u32(uint8_t* p, uint32_t v) { std::memcpy(p, &v, 4); }
inline void wr_u64(uint8_t* p, uint64_t v) { std::memcpy(p, &v, 8); }

inline int32_t rd_i32(const uint8_t* p) { int32_t v; std::memcpy(&v, p, 4); return v; }
inline void wr_i32(uint8_t* p, int32_t v) { std::memcpy(p, &v, 4); }
inline void wr_u128(uint8_t* p, u128 v) { std::memcpy(p, &v, 16); }

/** Decimal u64 (token amounts arrive as strings). */
inline bool parse_u64(std::string_view s, uint64_t& v) {
  if (s.empty() || s.size() > 20) return false;
  uint64_t r = 0;
  for (char c : s) {
    if (c < '0' || c > '9') return false;
    if (r > (UINT64_MAX - uint64_t(c - '0')) / 10) return false;
    r = r * 10 + uint64_t(c - '0');
  }
  v = r;
  return true;
}

static_assert(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__, "little-endian host required");

}  // namespace arb
