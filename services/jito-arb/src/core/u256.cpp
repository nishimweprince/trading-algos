#include "core/u256.hpp"

namespace arb {

// Knuth algorithm D over 32-bit digits (Hacker's Delight, divmnu).
void U256::divmod(const U256& dv, U256& q, U256& r) const {
  uint32_t u[8], v[8];
  for (int i = 0; i < 4; ++i) {
    u[2 * i] = uint32_t(w[i]);
    u[2 * i + 1] = uint32_t(w[i] >> 32);
    v[2 * i] = uint32_t(dv.w[i]);
    v[2 * i + 1] = uint32_t(dv.w[i] >> 32);
  }
  int m = 8, n = 8;
  while (m > 0 && u[m - 1] == 0) --m;
  while (n > 0 && v[n - 1] == 0) --n;
  q = U256{};
  r = U256{};
  if (n == 0) return;  // divide by zero: callers never do this
  if (m < n) {
    r = *this;
    return;
  }
  uint32_t qd[8] = {0};
  uint32_t rd[8] = {0};
  if (n == 1) {
    uint64_t k = 0;
    for (int j = m - 1; j >= 0; --j) {
      uint64_t cur = (k << 32) | u[j];
      qd[j] = uint32_t(cur / v[0]);
      k = cur % v[0];
    }
    rd[0] = uint32_t(k);
  } else {
    int s = __builtin_clz(v[n - 1]);
    uint32_t vn[8], un[9];
    for (int i = n - 1; i > 0; --i) vn[i] = (v[i] << s) | (s ? uint32_t(uint64_t(v[i - 1]) >> (32 - s)) : 0);
    vn[0] = v[0] << s;
    un[m] = s ? uint32_t(uint64_t(u[m - 1]) >> (32 - s)) : 0;
    for (int i = m - 1; i > 0; --i) un[i] = (u[i] << s) | (s ? uint32_t(uint64_t(u[i - 1]) >> (32 - s)) : 0);
    un[0] = u[0] << s;
    const uint64_t b = 1ULL << 32;
    for (int j = m - n; j >= 0; --j) {
      uint64_t num = (uint64_t(un[j + n]) << 32) | un[j + n - 1];
      uint64_t qhat = num / vn[n - 1];
      uint64_t rhat = num % vn[n - 1];
      while (qhat >= b || qhat * vn[n - 2] > ((rhat << 32) | un[j + n - 2])) {
        --qhat;
        rhat += vn[n - 1];
        if (rhat >= b) break;
      }
      int64_t t = 0, k = 0;
      for (int i = 0; i < n; ++i) {
        uint64_t p = qhat * vn[i];
        t = int64_t(un[i + j]) - k - int64_t(p & 0xffffffffULL);
        un[i + j] = uint32_t(t);
        k = int64_t(p >> 32) - (t >> 32);
      }
      t = int64_t(un[j + n]) - k;
      un[j + n] = uint32_t(t);
      qd[j] = uint32_t(qhat);
      if (t < 0) {
        --qd[j];
        k = 0;
        for (int i = 0; i < n; ++i) {
          t = int64_t(un[i + j]) + vn[i] + k;
          un[i + j] = uint32_t(t);
          k = t >> 32;
        }
        un[j + n] = uint32_t(int64_t(un[j + n]) + k);
      }
    }
    for (int i = 0; i < n; ++i)
      rd[i] = s ? (un[i] >> s) | uint32_t(uint64_t(un[i + 1]) << (32 - s)) : un[i];
  }
  for (int i = 0; i < 4; ++i) {
    q.w[i] = uint64_t(qd[2 * i]) | (uint64_t(qd[2 * i + 1]) << 32);
    r.w[i] = uint64_t(rd[2 * i]) | (uint64_t(rd[2 * i + 1]) << 32);
  }
}

}  // namespace arb
