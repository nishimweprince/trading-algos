#include "sol/crypto.hpp"

#include <sodium.h>

#include <cstring>
#include <stdexcept>
#include <vector>

#include "core/base58.hpp"
#include "sol/constants.hpp"

namespace pd {

void crypto_init() {
  if (sodium_init() < 0) throw std::runtime_error("sodium_init failed");
}

// ---------------------------------------------------------------------------
// GF(2^255-19) in 16 signed 64-bit limbs of 16 bits (TweetNaCl).
namespace {
using gf = int64_t[16];
const gf gf1 = {1};
const gf D = {0x78a3, 0x1359, 0x4dca, 0x75eb, 0xd8ab, 0x4141, 0x0a4d, 0x0070,
              0xe898, 0x7779, 0x4079, 0x8cc7, 0xfe73, 0x2b6f, 0x6cee, 0x5203};
const gf I = {0xa0b0, 0x4a0e, 0x1b27, 0xc4ee, 0xe478, 0xad2f, 0x1806, 0x2f43,
              0xd7a7, 0x3dfb, 0x0099, 0x2b4d, 0xdf0b, 0x4fc1, 0x2480, 0x2b83};

void set25519(gf r, const gf a) { for (int i = 0; i < 16; ++i) r[i] = a[i]; }

void car25519(gf o) {
  for (int i = 0; i < 16; ++i) {
    o[i] += (int64_t(1) << 16);
    int64_t c = o[i] >> 16;
    o[(i + 1) * (i < 15)] += c - 1 + 37 * (c - 1) * (i == 15);
    o[i] -= c * (int64_t(1) << 16);
  }
}

void sel25519(gf p, gf q, int b) {
  int64_t c = ~(int64_t(b) - 1);
  for (int i = 0; i < 16; ++i) {
    int64_t t = c & (p[i] ^ q[i]);
    p[i] ^= t;
    q[i] ^= t;
  }
}

void pack25519(uint8_t* o, const gf n) {
  gf m, t;
  for (int i = 0; i < 16; ++i) t[i] = n[i];
  car25519(t);
  car25519(t);
  car25519(t);
  for (int j = 0; j < 2; ++j) {
    m[0] = t[0] - 0xffed;
    for (int i = 1; i < 15; ++i) {
      m[i] = t[i] - 0xffff - ((m[i - 1] >> 16) & 1);
      m[i - 1] &= 0xffff;
    }
    m[15] = t[15] - 0x7fff - ((m[14] >> 16) & 1);
    int b = static_cast<int>((m[15] >> 16) & 1);
    m[14] &= 0xffff;
    sel25519(t, m, 1 - b);
  }
  for (int i = 0; i < 16; ++i) {
    o[2 * i] = static_cast<uint8_t>(t[i] & 0xff);
    o[2 * i + 1] = static_cast<uint8_t>((t[i] >> 8) & 0xff);
  }
}

bool neq25519(const gf a, const gf b) {
  uint8_t c[32], d[32];
  pack25519(c, a);
  pack25519(d, b);
  return std::memcmp(c, d, 32) != 0;
}

void unpack25519(gf o, const uint8_t* n) {
  for (int i = 0; i < 16; ++i) o[i] = n[2 * i] + (int64_t(n[2 * i + 1]) << 8);
  o[15] &= 0x7fff;
}

void A(gf o, const gf a, const gf b) { for (int i = 0; i < 16; ++i) o[i] = a[i] + b[i]; }
void Z(gf o, const gf a, const gf b) { for (int i = 0; i < 16; ++i) o[i] = a[i] - b[i]; }

void M(gf o, const gf a, const gf b) {
  int64_t t[31] = {0};
  for (int i = 0; i < 16; ++i)
    for (int j = 0; j < 16; ++j) t[i + j] += a[i] * b[j];
  for (int i = 0; i < 15; ++i) t[i] += 38 * t[i + 16];
  for (int i = 0; i < 16; ++i) o[i] = t[i];
  car25519(o);
  car25519(o);
}
void S(gf o, const gf a) { M(o, a, a); }

void pow2523(gf o, const gf i) {
  gf c;
  for (int a = 0; a < 16; ++a) c[a] = i[a];
  for (int a = 250; a >= 0; --a) {
    S(c, c);
    if (a != 1) M(c, c, i);
  }
  for (int a = 0; a < 16; ++a) o[a] = c[a];
}
}  // namespace

bool is_on_curve(const uint8_t p[32]) {
  gf r0, r1, r2, t, chk, num, den, den2, den4, den6;
  set25519(r2, gf1);
  unpack25519(r1, p);
  S(num, r1);
  M(den, num, D);
  Z(num, num, r2);
  A(den, r2, den);

  S(den2, den);
  S(den4, den2);
  M(den6, den4, den2);
  M(t, den6, num);
  M(t, t, den);

  pow2523(t, t);
  M(t, t, num);
  M(t, t, den);
  M(t, t, den);
  M(r0, t, den);

  S(chk, r0);
  M(chk, chk, den);
  if (neq25519(chk, num)) M(r0, r0, I);

  S(chk, r0);
  M(chk, chk, den);
  if (neq25519(chk, num)) return false;
  return true;
}

// ---------------------------------------------------------------------------

bool find_program_address(std::initializer_list<Seed> seeds, const Pubkey& program, Pubkey& out, uint8_t* bump_out) {
  static constexpr char kMarker[] = "ProgramDerivedAddress";
  for (int bump = 255; bump >= 0; --bump) {
    crypto_hash_sha256_state st;
    crypto_hash_sha256_init(&st);
    for (const Seed& s : seeds) crypto_hash_sha256_update(&st, s.ptr, s.len);
    uint8_t b = static_cast<uint8_t>(bump);
    crypto_hash_sha256_update(&st, &b, 1);
    crypto_hash_sha256_update(&st, program.data(), 32);
    crypto_hash_sha256_update(&st, reinterpret_cast<const uint8_t*>(kMarker), sizeof(kMarker) - 1);
    crypto_hash_sha256_final(&st, out.data());
    if (!is_on_curve(out.data())) {
      if (bump_out) *bump_out = b;
      return true;
    }
  }
  return false;
}

Pubkey pda(std::initializer_list<Seed> seeds, const Pubkey& program) {
  Pubkey out;
  if (!find_program_address(seeds, program, out)) throw std::runtime_error("no viable PDA bump");
  return out;
}

Pubkey ata(const Pubkey& owner, const Pubkey& mint, const Pubkey& token_program) {
  return pda({owner, token_program, mint}, prog::ATA);
}

Keypair Keypair::from_seed(const uint8_t seed[32]) {
  Keypair kp;
  crypto_sign_ed25519_seed_keypair(kp.pub_.data(), kp.sk_, seed);
  return kp;
}

Keypair Keypair::parse(std::string_view secret) {
  while (!secret.empty() && (secret.back() == '\n' || secret.back() == ' ' || secret.back() == '\r')) secret.remove_suffix(1);
  std::array<uint8_t, 64> raw{};
  if (!secret.empty() && secret.front() == '[') {
    size_t n = 0;
    int cur = -1;
    for (char c : secret) {
      if (c >= '0' && c <= '9') cur = (cur < 0 ? 0 : cur * 10) + (c - '0');
      else if (cur >= 0) {
        if (n >= 64 || cur > 255) throw std::runtime_error("wallet key: bad JSON byte array");
        raw[n++] = static_cast<uint8_t>(cur);
        cur = -1;
      }
    }
    if (n != 64) throw std::runtime_error("wallet key: JSON array must hold 64 bytes");
  } else if (!b58_decode_fixed(secret, raw)) {
    throw std::runtime_error("wallet key: not a base58 64-byte secret");
  }
  Keypair kp = from_seed(raw.data());
  if (std::memcmp(kp.pub_.data(), raw.data() + 32, 32) != 0)
    throw std::runtime_error("wallet key: public half does not match the seed");
  sodium_memzero(raw.data(), raw.size());
  return kp;
}

void Keypair::sign(const uint8_t* msg, size_t len, uint8_t sig_out[64]) const {
  crypto_sign_ed25519_detached(sig_out, nullptr, msg, len, sk_);
}

}  // namespace pd
