#include "core/base58.hpp"

namespace pd {

size_t b58_encode(const uint8_t* data, size_t n, char* out) {
  static constexpr char alpha[] = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
  size_t zeros = 0;
  while (zeros < n && data[zeros] == 0) ++zeros;
  uint8_t digits[160] = {0};  // base58 digits, little-endian; 64 bytes -> <= 88 digits
  size_t len = 0;
  for (size_t i = zeros; i < n; ++i) {
    uint32_t carry = data[i];
    for (size_t j = 0; j < len; ++j) {
      carry += static_cast<uint32_t>(digits[j]) << 8;
      digits[j] = static_cast<uint8_t>(carry % 58);
      carry /= 58;
    }
    while (carry) {
      digits[len++] = static_cast<uint8_t>(carry % 58);
      carry /= 58;
    }
  }
  size_t o = 0;
  for (size_t i = 0; i < zeros; ++i) out[o++] = '1';
  for (size_t i = len; i-- > 0;) out[o++] = alpha[digits[i]];
  return o;
}

}  // namespace pd
