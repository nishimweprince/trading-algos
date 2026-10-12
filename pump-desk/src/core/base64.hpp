#pragma once
#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace pd {

constexpr size_t b64_len(size_t n) { return ((n + 2) / 3) * 4; }

/** Standard base64 with padding into `out` (capacity b64_len(n)). No allocation. */
inline size_t b64_encode(const uint8_t* in, size_t n, char* out) {
  static constexpr char t[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  size_t o = 0, i = 0;
  for (; i + 2 < n; i += 3) {
    uint32_t v = (uint32_t(in[i]) << 16) | (uint32_t(in[i + 1]) << 8) | in[i + 2];
    out[o++] = t[(v >> 18) & 63];
    out[o++] = t[(v >> 12) & 63];
    out[o++] = t[(v >> 6) & 63];
    out[o++] = t[v & 63];
  }
  if (i < n) {
    uint32_t v = uint32_t(in[i]) << 16;
    if (i + 1 < n) v |= uint32_t(in[i + 1]) << 8;
    out[o++] = t[(v >> 18) & 63];
    out[o++] = t[(v >> 12) & 63];
    out[o++] = (i + 1 < n) ? t[(v >> 6) & 63] : '=';
    out[o++] = '=';
  }
  return o;
}

/** Off-hot-path decode (RPC responses, fixtures). Skips whitespace; false on bad input. */
inline bool b64_decode(std::string_view s, std::vector<uint8_t>& out) {
  auto val = [](char c) -> int {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+' || c == '-') return 62;
    if (c == '/' || c == '_') return 63;
    return -1;
  };
  out.clear();
  out.reserve(s.size() * 3 / 4);
  uint32_t acc = 0;
  int bits = 0;
  for (char c : s) {
    if (c == '=' || c == '\n' || c == '\r' || c == ' ') continue;
    int v = val(c);
    if (v < 0) return false;
    acc = (acc << 6) | static_cast<uint32_t>(v);
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out.push_back(static_cast<uint8_t>((acc >> bits) & 0xff));
    }
  }
  return true;
}

inline std::string b64(const uint8_t* in, size_t n) {
  std::string s(b64_len(n), '\0');
  s.resize(b64_encode(in, n, s.data()));
  return s;
}

}  // namespace pd
