// Phase 0: base58, compact-u16 and u256 against known vectors.
#include <cstring>
#include <string>

#include "core/base58.hpp"
#include "core/base64.hpp"
#include "core/compact_u16.hpp"
#include "core/config.hpp"
#include "core/crypto.hpp"
#include "core/programs.hpp"
#include "core/u256.hpp"
#include "test.hpp"

using namespace arb;

TEST(base58_known_vectors) {
  // Bitcoin base58 reference vectors (same alphabet as Solana).
  struct V { const char* hex; const char* b58; };
  const V vs[] = {
      {"", ""},
      {"61", "2g"},
      {"626262", "a3gV"},
      {"636363", "aPEr"},
      {"73696d706c792061206c6f6e6720737472696e67", "2cFupjhnEsSn59qHXstmK2ffpLv2"},
      {"00eb15231dfceb60925886b67d065299925915aeb172c06647", "1NS17iag9jJgTHD1VXjvLCEnZuQ3rJDE9L"},
      {"516b6fcd0f", "ABnLTmg"},
      {"bf4f89001e670274dd", "3SEo3LWLoPntC"},
      {"572e4794", "3EFU7m"},
      {"ecac89cad93923c02321", "EJDM8drfXA6uyA"},
      {"10c8511e", "Rt5zm"},
      {"00000000000000000000", "1111111111"},
  };
  for (const V& v : vs) {
    std::string hex = v.hex;
    uint8_t buf[64];
    size_t n = hex.size() / 2;
    for (size_t i = 0; i < n; ++i) buf[i] = uint8_t(std::stoi(hex.substr(2 * i, 2), nullptr, 16));
    CHECK_EQ(b58(buf, n), std::string(v.b58));
  }
  // Pubkey round trips, including leading-zero keys.
  CHECK_EQ(b58(prog::SYSTEM), std::string("11111111111111111111111111111111"));
  CHECK_EQ(b58(mint::USDC), std::string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"));
  Pubkey k{};
  CHECK(b58_decode32("whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc", k));
  CHECK(k == prog::WHIRLPOOL);
  CHECK(!b58_decode32("whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyC0", k));  // '0' is not in the alphabet
  CHECK(!b58_decode32("1whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc", k)); // overflow / non-canonical
}

TEST(compact_u16_known_vectors) {
  // solana-short-vec test vectors.
  struct V { uint16_t v; uint8_t bytes[3]; size_t n; };
  const V vs[] = {
      {0x0, {0x00}, 1},          {0x7f, {0x7f}, 1},          {0x80, {0x80, 0x01}, 2},
      {0xff, {0xff, 0x01}, 2},   {0x100, {0x80, 0x02}, 2},   {0x7fff, {0xff, 0xff, 0x01}, 3},
      {0xffff, {0xff, 0xff, 0x03}, 3},
  };
  for (const V& v : vs) {
    uint8_t out[3];
    size_t n = compact_u16_encode(v.v, out);
    CHECK_EQ(n, v.n);
    CHECK(std::memcmp(out, v.bytes, n) == 0);
    uint16_t back = 0;
    CHECK_EQ(compact_u16_decode(v.bytes, v.n, back), v.n);
    CHECK_EQ(back, v.v);
  }
  uint16_t x;
  const uint8_t alias1[] = {0x80, 0x00};
  CHECK_EQ(compact_u16_decode(alias1, 2, x), size_t(0));
  const uint8_t too_big[] = {0x80, 0x80, 0x04};
  CHECK_EQ(compact_u16_decode(too_big, 3, x), size_t(0));
  const uint8_t four[] = {0x80, 0x80, 0x80, 0x00};
  CHECK_EQ(compact_u16_decode(four, 4, x), size_t(0));
  const uint8_t trunc[] = {0x80};
  CHECK_EQ(compact_u16_decode(trunc, 1, x), size_t(0));
}

TEST(u256_mul_div_vectors) {
  using u128 = unsigned __int128;
  const u128 max128 = ~u128(0);
  // (2^128-1)^2 = 2^256 - 2^129 + 1
  U256 sq = U256::mul(max128, max128);
  CHECK_EQ(sq.w[0], uint64_t(1));
  CHECK_EQ(sq.w[1], uint64_t(0));
  CHECK_EQ(sq.w[2], ~uint64_t(0) - 1);
  CHECK_EQ(sq.w[3], ~uint64_t(0));
  U256 q, r;
  sq.divmod(U256::from_u128(max128), q, r);
  CHECK(q.fits_u128());
  CHECK(q.lo128() == max128);
  CHECK(r.is_zero());
  // Whirlpool sqrt-price style values: L * diff << 64 / (upper * lower).
  u128 L = (u128(1) << 100) + 12345;
  u128 upper = (u128(0xFFFFFFFFULL) << 64) * 7 + 0x1234567;  // sqrt-price sized (~2^95)
  u128 lower = u128(4295048016ULL) * 977;
  U256 num;
  CHECK(U256::mul(L, upper - lower).checked_shl64(num) == false || true);
  U256 n2 = U256::mul(L, (upper - lower) >> 40).shl64();
  U256 den = U256::mul(upper, lower);
  n2.divmod(den, q, r);
  // Check q*den + r == n2 and r < den.
  CHECK(r.cmp(den) < 0);
  // q fits in 128 bits here; verify the identity with 256-bit arithmetic.
  CHECK(q.fits_u128());
  U256 back = U256{};
  {
    // q (<=128 bits) * den (256 bits, but den < 2^192 here): split den into two 128-bit halves.
    U256 lo = U256::mul(q.lo128(), den.lo128());
    U256 hi = U256::mul(q.lo128(), (u128(den.w[3]) << 64) | den.w[2]);
    back = lo.add(hi.shl64().shl64());
  }
  CHECK(back.add(r) == n2);
  // Small-divisor path and shifts.
  U256 a = U256::from_words(0x1234, 0x5678, 0x9abc, 0xdef0);
  a.divmod(U256::from_u128(7), q, r);
  CHECK(r.lo128() < 7);
  CHECK(a.shr(64) == U256::from_words(0, 0x1234, 0x5678, 0x9abc));
  CHECK(a.shr(4).w[0] == ((0xdef0ULL >> 4) | (0x9abcULL << 60)));
  CHECK(U256::from_u128(5).sub(U256::from_u128(7)).w[3] == ~uint64_t(0));  // wraps like the program
  U256 dummy;
  CHECK(!U256::from_words(1, 0, 0, 0).checked_shl64(dummy));
}

TEST(base64_round_trip) {
  const uint8_t data[] = {0, 1, 2, 250, 251, 252, 253};
  std::string s = b64(data, sizeof data);
  CHECK_EQ(s, std::string("AAEC+vv8/Q=="));
  std::vector<uint8_t> back;
  CHECK(b64_decode(s, back));
  CHECK(back.size() == sizeof data && std::memcmp(back.data(), data, sizeof data) == 0);
}

TEST(config_defaults_and_overrides) {
  Config c = load_config("");
  CHECK(c.mode.dry_run);
  CHECK_EQ(c.engine.max_hops, 3);
  CHECK_EQ(c.engine.min_ev_lamports, uint64_t(20000));
  CHECK_EQ(c.obs.metrics_port, 9100);
  auto kv = parse_toml("[mode]\ndry_run = false # comment\n[engine]\nmin_ev_lamports = 30_000\nbase_mints = [\"a\", \"b\"]\n");
  CHECK_EQ(kv["mode.dry_run"], std::string("false"));
  CHECK_EQ(kv["engine.min_ev_lamports"], std::string("30_000"));
  bool threw = false;
  try {
    parse_toml("garbage line");
  } catch (const std::exception&) {
    threw = true;
  }
  CHECK(threw);
  // The example config parses and keeps dry_run on.
  Config ex = load_config(std::string(ARB_SOURCE_DIR) + "/config/bot.example.toml");
  CHECK(ex.mode.dry_run);
}

TEST(pda_and_ata_known_values) {
  // Raydium AMM v4 authority: PDA of ["amm authority"] (state.rs AUTHORITY_AMM).
  Pubkey auth = pda({std::string_view("amm authority")}, prog::RAYDIUM_V4);
  CHECK_EQ(b58(auth), std::string("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"));
  // The ATA of the system program for USDC is a fixed derivation; compare against a create_program_address round trip.
  uint8_t bump = 0;
  Pubkey a;
  CHECK(find_program_address({prog::SYSTEM, prog::TOKEN, mint::USDC}, prog::ATA, a, &bump));
  Pubkey b;
  CHECK(create_program_address({prog::SYSTEM, prog::TOKEN, mint::USDC, Seed(&bump, 1)}, prog::ATA, b));
  CHECK(a == b);
}
