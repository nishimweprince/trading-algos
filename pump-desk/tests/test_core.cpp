#include <thread>

#include "core/base58.hpp"
#include "core/config.hpp"
#include "core/base64.hpp"
#include "core/key_map.hpp"
#include "core/pb.hpp"
#include "core/ring.hpp"
#include "sol/constants.hpp"
#include "sol/layouts.hpp"
#include "test.hpp"

using namespace pd;

TEST(base58_roundtrip) {
  CHECK_EQ(b58(prog::PUMP_SWAP), std::string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"));
  CHECK_EQ(b58(prog::SYSTEM), std::string("11111111111111111111111111111111"));
  Pubkey k;
  CHECK(b58_decode32("So11111111111111111111111111111111111111112", k));
  CHECK(k == prog::WSOL);
  CHECK(!b58_decode32("So1111111111111111111111111111111111111111O", k));  // 'O' is not base58
  CHECK(!b58_decode32("2", k));  // not canonical for 32 bytes
  Signature s{};
  for (int i = 0; i < 64; ++i) s[i] = uint8_t(i * 7 + 1);
  std::array<uint8_t, 64> back{};
  CHECK(b58_decode_fixed(b58(s), back));
  CHECK(back == s);
}

TEST(base64_roundtrip) {
  for (size_t n = 0; n < 20; ++n) {
    std::vector<uint8_t> in(n);
    for (size_t i = 0; i < n; ++i) in[i] = uint8_t(i * 37 + n);
    std::string enc = b64(in.data(), in.size());
    CHECK_EQ(enc.size(), b64_len(n));
    std::vector<uint8_t> out;
    CHECK(b64_decode(enc, out));
    CHECK(out == in);
  }
  CHECK_EQ(b64(reinterpret_cast<const uint8_t*>("hello"), 5), std::string("aGVsbG8="));
}

TEST(pb_roundtrip) {
  pb::Writer inner;
  inner.u64(1, 300);
  inner.bytes(2, "abc");
  pb::Writer w;
  w.message(4, inner);
  w.u64(7, 1ULL << 40);
  pb::Reader r(w.out);
  uint32_t f, wt;
  int seen = 0;
  while (r.next(f, wt)) {
    if (f == 4 && wt == pb::LEN) {
      pb::Reader in(r.bytes());
      while (in.next(f, wt)) {
        if (f == 1) CHECK_EQ(in.varint(), uint64_t(300));
        else if (f == 2) CHECK(in.bytes() == "abc");
        else in.skip(wt);
      }
      ++seen;
    } else if (f == 7) {
      CHECK_EQ(r.varint(), uint64_t(1) << 40);
      ++seen;
    } else {
      r.skip(wt);
    }
  }
  CHECK(r.ok);
  CHECK_EQ(seen, 2);
  const uint8_t bad_bytes[] = {0x0a, 0x05, 0x61, 0x62};
  pb::Reader bad(bad_bytes, 4);
  CHECK(bad.next(f, wt));
  bad.bytes();
  CHECK(!bad.ok);
}

TEST(key_map_insert_erase) {
  KeyMap<int> m(64);
  std::vector<Pubkey> keys;
  for (int i = 0; i < 40; ++i) {
    Pubkey k{};
    k[0] = uint8_t(i & 3);  // force collisions: only 4 home buckets... before mixing
    k[7] = uint8_t(i);
    k[31] = uint8_t(i);
    keys.push_back(k);
    bool created = false;
    int* v = m.upsert(k, &created);
    CHECK(v != nullptr);
    CHECK(created);
    *v = i;
  }
  CHECK(m.upsert(keys[0]) != nullptr);
  for (int i = 0; i < 40; i += 2) CHECK(m.erase(keys[i]));
  for (int i = 0; i < 40; ++i) {
    int* v = m.find(keys[i]);
    if (i % 2 == 0) CHECK(v == nullptr);
    else { CHECK(v != nullptr); if (v) CHECK_EQ(*v, i); }
  }
  CHECK_EQ(m.size(), size_t(20));
}

TEST(spsc_threads) {
  static Spsc<uint64_t, 1024> q;
  constexpr uint64_t N = 200000;
  std::thread prod([] {
    for (uint64_t i = 0; i < N;) if (q.push(i)) ++i;
  });
  uint64_t expect = 0, v;
  while (expect < N) if (q.try_pop(v)) { if (v != expect) { CHECK_EQ(v, expect); break; } ++expect; }
  prod.join();
  CHECK_EQ(expect, N);
}

TEST(frame_ring_gating_and_lossy_reader) {
  FrameRing ring(8, 64);
  constexpr uint64_t N = 50000;
  std::thread prod([&] {
    for (uint64_t i = 0; i < N; ++i) {
      uint64_t seq;
      uint8_t* d = ring.claim(seq);
      std::memcpy(d, &i, 8);
      ring.publish(seq, 8, i);
    }
  });
  uint64_t seq = 0;
  while (seq < N) {
    const uint8_t* d;
    uint32_t len;
    uint64_t t;
    if (!ring.peek(seq, d, len, t)) continue;
    uint64_t v;
    std::memcpy(&v, d, 8);
    if (v != seq || len != 8) { CHECK_EQ(v, seq); break; }
    ring.release(++seq);
  }
  prod.join();
  uint8_t buf[64];
  uint32_t len;
  uint64_t t;
  CHECK_EQ(ring.copy_out(N - 1, buf, len, t), 1);
  CHECK_EQ(ring.copy_out(0, buf, len, t), -1);
  CHECK_EQ(ring.copy_out(N, buf, len, t), 0);
}

TEST(curve_completion_cost) {
  Curve c;
  c.virtual_token = 300'000'000'000'000ULL;
  c.virtual_sol = 100'000'000'000ULL;
  c.real_token = 20'000'000'000'000ULL;
  double cost = completion_cost_sol(c, 125);
  // k/(vT-real) - vS = 100e9*300/280 - 100e9 = 7.142857e9 lamports, / (1 - 0.0125)
  CHECK(cost > 7.23 && cost < 7.24);
  c.complete = true;
  CHECK_EQ(completion_cost_sol(c, 125), 0.0);
}

TEST(mint_transfer_fee_tlv) {
  uint8_t d[400] = {0};
  MintInfo spl;
  CHECK(decode_mint(d, 82, false, spl));
  CHECK(spl.fee == TransferFee::None);
  MintInfo m;
  // bare T22 mint
  CHECK(decode_mint(d, 166, true, m));
  CHECK(m.fee == TransferFee::None);
  // MetadataPointer (type 18, len 64) then TransferFeeConfig with zero fees, no authority
  size_t o = 166;
  wr_u16(d + o, 18); wr_u16(d + o + 2, 64); o += 4 + 64;
  wr_u16(d + o, 1); wr_u16(d + o + 2, 108); o += 4 + 108;
  CHECK(decode_mint(d, o, true, m));
  CHECK(m.fee == TransferFee::None);
  wr_u16(d + 166 + 68 + 4 + 106, 50);  // newer bps = 0.5 %
  CHECK(decode_mint(d, o, true, m));
  CHECK(m.fee == TransferFee::Present);
}

TEST(config_example_parses_to_defaults) {
  Config d;
  Config c = load_config(std::string(PD_FIXTURE_DIR) + "/../../config.example.toml");
  CHECK_EQ(c.racer.max_completion_cost_sol, d.racer.max_completion_cost_sol);
  CHECK_EQ(c.entry.size_wallet_pct, d.entry.size_wallet_pct);
  CHECK_EQ(c.exits.tp1_pct, d.exits.tp1_pct);
  CHECK_EQ(c.exits.take_profit_slippage_tiers.size(), size_t(3));
  CHECK_EQ(c.sender.url, d.sender.url);
  CHECK_EQ(c.sender.tip_accounts.size(), size_t(10));
  CHECK_EQ(c.racer.default_virtual_quote_lamports, uint64_t(17'584'492'770ULL));
  CHECK(!c.jito.enabled);
  bool threw = false;
  try {
    auto kv = parse_toml("[entry]\nsize_wallet_pctt = 3\n");
    (void)kv;
    // unknown keys are rejected by load_config, not parse_toml
  } catch (...) { threw = true; }
  CHECK(!threw);
}
