#pragma once
// Solana's compact-u16 ("shortvec") length prefix: 7 bits per byte, little-endian, 1–3 bytes.
#include <cstddef>
#include <cstdint>

namespace arb {

/** Encode `v` (<= 0xffff) into out; returns bytes written (1..3). */
inline size_t compact_u16_encode(uint16_t v, uint8_t* out) {
  size_t n = 0;
  uint32_t x = v;
  for (;;) {
    uint8_t b = x & 0x7f;
    x >>= 7;
    if (x == 0) { out[n++] = b; return n; }
    out[n++] = b | 0x80;
  }
}

/**
 * Decode at p (len bytes available). Returns bytes consumed, 0 on malformed input:
 * truncated, more than 3 bytes, a value above 0xffff, or a non-minimal (alias) encoding,
 * matching solana-short-vec's rejections.
 */
inline size_t compact_u16_decode(const uint8_t* p, size_t len, uint16_t& v) {
  uint32_t val = 0;
  for (size_t i = 0; i < 3; ++i) {
    if (i >= len) return 0;
    uint8_t b = p[i];
    if (i == 2 && (b & 0x80)) return 0;
    if (i > 0 && b == 0) return 0;  // alias: a zero continuation byte
    val |= uint32_t(b & 0x7f) << (7 * i);
    if (!(b & 0x80)) {
      if (val > 0xffff) return 0;
      v = static_cast<uint16_t>(val);
      return i + 1;
    }
  }
  return 0;
}

}  // namespace arb
