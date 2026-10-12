#pragma once
// Little-endian readers/writers over raw account bytes.
#include <cstdint>
#include <cstring>

namespace pd {

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

static_assert(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__, "little-endian host required");

}  // namespace pd
