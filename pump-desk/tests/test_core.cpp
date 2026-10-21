#include <filesystem>
#include <fstream>
#include <sstream>
#include <thread>

#include "core/base58.hpp"
#include "core/config.hpp"
#include "core/base64.hpp"
#include "core/key_map.hpp"
#include "core/pb.hpp"
#include "core/ring.hpp"
#include "exec/exec.hpp"
#include "stream/request.hpp"
#include "stream/update.hpp"
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

TEST(classify_sender_responses) {
  SendVerdict v = classify_response(200, R"({"jsonrpc":"2.0","result":"5abc","id":1})");
  CHECK(v.ok && !v.definitive && !v.rate_limited);
  v = classify_response(200, R"({"jsonrpc":"2.0","error":{"code":-32002},"id":1})");
  CHECK(!v.ok && v.definitive && !v.rate_limited);
  v = classify_response(429, "<html><body><h1>429 Too Many Requests</h1>");
  CHECK(!v.ok && !v.definitive && v.rate_limited);
  v = classify_response(400, "bad request");
  CHECK(!v.ok && v.definitive && !v.rate_limited);
  v = classify_response(503, "");
  CHECK(!v.ok && !v.definitive && !v.rate_limited);
  v = classify_response(0, "connection closed");
  CHECK(!v.ok && !v.definitive && !v.rate_limited);
}

TEST(parse_tx_index_and_slot_status) {
  // SubscribeUpdate { filters 1, transaction 4 { transaction 1 { signature 1, index 5 }, slot 2 } }
  pb::Writer info;
  info.bytes(1, std::string(64, 'x'));
  info.u64(5, 7);
  pb::Writer upd;
  upd.message(1, info);
  upd.u64(2, 42);
  pb::Writer root;
  root.bytes(1, "wallet");
  root.message(4, upd);
  UpdateView* v = new UpdateView;
  CHECK(parse_update(reinterpret_cast<const uint8_t*>(root.out.data()), root.out.size(), *v));
  CHECK(v->kind == UpdateView::Transaction);
  CHECK_EQ(v->tx.index, int64_t(7));
  CHECK_EQ(v->tx.slot, uint64_t(42));

  pb::Writer sl;
  sl.u64(1, 43);
  sl.u64(3, 3);
  pb::Writer r2;
  r2.bytes(1, "heartbeat");
  r2.message(3, sl);
  CHECK(parse_update(reinterpret_cast<const uint8_t*>(r2.out.data()), r2.out.size(), *v));
  CHECK(v->kind == UpdateView::Slot);
  CHECK_EQ(v->slot.status, uint32_t(3));
  delete v;
}

TEST(subscribe_requests_interslot_updates) {
  SubscriptionSpec spec;
  std::string req = encode_subscribe_request(spec);
  // slots map entry "heartbeat" -> { filter_by_commitment 1: true, interslot_updates 2: true } = 08 01 10 01
  const std::string want = std::string("heartbeat") + "\x12\x04\x08\x01\x10\x01";
  CHECK(req.find(want) != std::string::npos);
}

TEST(config_example_parses_to_defaults) {
  Config d;
  Config c = load_config(std::string(PD_FIXTURE_DIR) + "/../../config.example.toml");
  CHECK_EQ(c.curve.entry_max_remaining_sol, d.curve.entry_max_remaining_sol);
  CHECK_EQ(c.curve.max_hold_s, d.curve.max_hold_s);
  CHECK_EQ(c.curve.allow_mayhem, d.curve.allow_mayhem);
  CHECK_EQ(c.curve.partial_fill, d.curve.partial_fill);
  CHECK_EQ(c.entry.size_sol, d.entry.size_sol);
  CHECK_EQ(c.entry.slippage_pct, d.entry.slippage_pct);
  CHECK_EQ(c.exits.tp_pct, d.exits.tp_pct);
  CHECK_EQ(c.exits.timeout_slots, d.exits.timeout_slots);
  CHECK_EQ(c.exits.exit_priority_multiplier, d.exits.exit_priority_multiplier);
  CHECK_EQ(c.risk.max_concurrent_positions, d.risk.max_concurrent_positions);
  CHECK_EQ(c.risk.max_loss_sol, d.risk.max_loss_sol);
  CHECK_EQ(c.routing.agave.cu_price_mode, d.routing.agave.cu_price_mode);
  CHECK_EQ(c.exits.take_profit_slippage_tiers.size(), size_t(3));
  CHECK_EQ(c.sender.url, d.sender.url);
  CHECK_EQ(c.sender.tip_accounts.size(), size_t(10));
  CHECK_EQ(c.curve.default_virtual_quote_lamports, uint64_t(17'584'492'770ULL));
  CHECK(!c.jito.enabled);
  CHECK_EQ(c.sender.retry_429_ms, d.sender.retry_429_ms);
  CHECK_EQ(c.sender.max_429_retries, d.sender.max_429_retries);
  CHECK_EQ(c.fees.priority_level, d.fees.priority_level);
  CHECK_EQ(c.entry.priority_multiplier, d.entry.priority_multiplier);
  CHECK_EQ(c.curve.overhang_top_k, d.curve.overhang_top_k);
  CHECK_EQ(c.curve.overhang_max_drop_pct, d.curve.overhang_max_drop_pct);
  CHECK_EQ(c.curve.overhang_refresh_ms, d.curve.overhang_refresh_ms);
  CHECK_EQ(c.curve.overhang_max_age_ms, d.curve.overhang_max_age_ms);
  {
    // An out-of-range overhang threshold refuses the start.
    std::ifstream in(std::string(PD_FIXTURE_DIR) + "/../../config.example.toml");
    std::stringstream ss;
    ss << in.rdbuf();
    std::string text = ss.str();
    const std::string from = "overhang_max_drop_pct = 0 ";
    size_t at = text.find(from);
    CHECK(at != std::string::npos);
    text.replace(at, from.size(), "overhang_max_drop_pct = 120 ");
    std::string path = (std::filesystem::temp_directory_path() / "pd_overhang_bad.toml").string();
    std::ofstream(path) << text;
    bool rejected = false;
    try {
      load_config(path);
    } catch (const std::exception&) { rejected = true; }
    std::filesystem::remove(path);
    CHECK(rejected);
  }
  bool threw = false;
  try {
    auto kv = parse_toml("[entry]\nsize_soll = 3\n");
    (void)kv;
    // unknown keys are rejected by load_config, not parse_toml
  } catch (...) { threw = true; }
  CHECK(!threw);
}
