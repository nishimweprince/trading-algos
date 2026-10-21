// Byte equality of the bonding-curve buy / sell against @pump-fun/pump-sdk + web3.js
// (tools/fixtures/gen_curve.mjs), and the curve maths against the SDK's quotes.
#include <cmath>

#include "fixture.hpp"
#include "sol/amm.hpp"
#include "sol/constants.hpp"
#include "sol/curve.hpp"
#include "sol/layouts.hpp"
#include "test.hpp"

using namespace pd;
using namespace pdtest;

namespace {

struct CurveEnv {
  AmmConfig cfg;
  Curve curve;
  Keypair kp;
  Pubkey mint, token_program, tip;
  Hash blockhash;
};

CurveEnv load_curve_env() {
  auto& g = curve_golden();
  CurveEnv e{};
  auto gl = b64bytes(g["accounts"]["pumpGlobal"]);
  auto cv = b64bytes(g["accounts"]["curve"]);
  CHECK(decode_pump_global(gl.data(), gl.size(), e.cfg));
  CHECK(decode_curve(cv.data(), cv.size(), e.curve));
  auto seed = unhex(std::string_view(g["userSeedHex"]));
  e.kp = Keypair::from_seed(seed.data());
  e.mint = key(g["liveMint"]);
  e.token_program = key(g["accounts"]["mintOwner"]);
  e.tip = key(g["tip"]);
  e.blockhash = key(g["blockhash"]);
  return e;
}

}  // namespace

TEST(curve_golden_decoders_and_pdas) {
  CurveEnv e = load_curve_env();
  auto& g = curve_golden();
  CHECK(e.cfg.curve_valid);
  CHECK_EQ(b58(e.cfg.curve_recipients[0]), str(g["recipients"]["fee"]));
  CHECK_EQ(b58(e.cfg.curve_reserved_recipients[0]), str(g["recipients"]["reserved"]));
  CHECK_EQ(b58(e.cfg.curve_buyback_recipients[0]), str(g["recipients"]["buyback"]));
  CHECK_EQ(b58(e.curve.creator), str(g["curve"]["creator"]));
  CHECK_EQ(e.curve.complete, bool(g["curve"]["complete"]));
  CHECK_EQ(e.curve.mayhem, bool(g["curve"]["mayhem"]));
  CHECK(curve_sol_quoted(e.curve));
  CHECK_EQ(b58(derived().pump_global), str(g["pdas"]["global"]));
  CHECK_EQ(b58(derived().pump_event_authority), str(g["pdas"]["eventAuthority"]));
  CHECK_EQ(b58(derived().pump_global_volume_acc), str(g["pdas"]["globalVolumeAccumulator"]));
  CHECK_EQ(b58(derived().pump_fee_config), str(g["pdas"]["feeConfig"]));
  CurveKeys k;
  derive_curve_keys(e.kp.pubkey(), e.mint, e.token_program, e.curve.creator, false, false, k);
  CHECK_EQ(b58(k.bonding_curve), str(g["pdas"]["bondingCurve"]));
  CHECK_EQ(b58(k.bonding_curve_v2), str(g["pdas"]["bondingCurveV2"]));
  CHECK_EQ(b58(k.creator_vault), str(g["pdas"]["creatorVault"]));
  CHECK_EQ(b58(k.user_volume_acc), str(g["pdas"]["userVolumeAccumulator"]));
  CHECK_EQ(b58(k.user_ata), str(g["pdas"]["userAta"]));
  // The migrated seed curve the boot self-check buys against.
  Curve seed;
  auto sc = b64bytes(g["accounts"]["seedCurve"]);
  CHECK(decode_curve(sc.data(), sc.size(), seed));
  CHECK(seed.complete);
}

TEST(curve_golden_messages) {
  CurveEnv e = load_curve_env();
  auto& g = curve_golden();
  TxParams p;
  p.cu_limit = uint32_t(int64_t(g["params"]["cuLimit"]));
  p.cu_price = uint64_t(int64_t(g["params"]["cuPrice"]));
  p.tips[0] = Tip{e.tip, uint64_t(int64_t(g["params"]["tip"]))};
  p.n_tips = 1;
  int n = 0;
  for (auto c : g["cases"].get_array()) {
    ++n;
    std::string name = str(c["name"]);
    bool sell = str(c["side"]) == "sell";
    bool mayhem = false, cashback = false, partial = false;
    (void)c["mayhem"].get(mayhem);
    (void)c["cashback"].get(cashback);
    (void)c["partial"].get(partial);
    uint64_t amount = std::stoull(str(c["amount"]));
    CurveKeys k;
    derive_curve_keys(e.kp.pubkey(), e.mint, e.token_program, e.curve.creator, mayhem, cashback, k);
    IxList l;
    if (!sell) curve_buy_ixs(l, e.kp.pubkey(), k, e.cfg, p, amount, std::stoull(str(c["maxSol"])), partial);
    else curve_sell_ixs(l, e.kp.pubkey(), k, e.cfg, p, amount, std::stoull(str(c["minSol"])), true);
    auto sdk_ixs = c["ixs"].get_array();
    CHECK_EQ(size_t(l.n), sdk_ixs.size() + 3);  // + CU limit, CU price, tip
    size_t i = 2;
    for (auto sx : sdk_ixs) {
      if (i >= l.n) break;
      const Ix& x = l.ixs[i++];
      CHECK_EQ(b58(x.program), str(sx["program"]));
      CHECK_EQ(hex(x.data, x.data_len), str(sx["data"]));
      auto keys = sx["keys"].get_array();
      CHECK_EQ(size_t(x.n_accounts), keys.size());
      size_t a = 0;
      for (auto kk : keys) {
        if (a >= x.n_accounts) break;
        const AccountMeta& m = x.accounts[a++];
        if (b58(m.key) != str(kk["pubkey"]) || m.signer != bool(kk["signer"]) || m.writable != bool(kk["writable"])) {
          std::fprintf(stderr, "  %s ix %zu acct %zu: ours %s s%d w%d, sdk %s s%d w%d\n", name.c_str(), i - 1, a - 1,
                       b58(m.key).c_str(), m.signer, m.writable, str(kk["pubkey"]).c_str(), int(bool(kk["signer"])),
                       int(bool(kk["writable"])));
          ++failures();
        }
      }
    }
    uint8_t msg[1300];
    size_t len = compile_message_v0(e.kp.pubkey(), l, e.blockhash, msg, sizeof(msg));
    CHECK(len > 0);
    if (hex(msg, len) != str(c["message"])) {
      std::fprintf(stderr, "  %s: message bytes differ (ours %zu bytes, sdk %zu)\n", name.c_str(), len,
                   str(c["message"]).size() / 2);
      ++failures();
    }
    SignedTx tx;
    CHECK(build_signed_tx(e.kp, l, e.blockhash, tx));
    CHECK(tx.len <= PACKET_DATA_SIZE);
  }
  CHECK(n >= 5);
}

TEST(curve_quotes_match_sdk) {
  CurveEnv e = load_curve_env();
  auto& g = curve_golden();
  uint64_t fee = std::stoull(str(g["fees"]["protocolFeeBps"])) + std::stoull(str(g["fees"]["creatorFeeBps"]));
  CHECK_EQ(curve_fee_bps(e.cfg, e.curve, 125), std::max<uint64_t>(fee, 125));
  for (auto q : g["quotes"].get_array()) {
    uint64_t lamports = std::stoull(str(q["lamports"]));
    uint64_t sdk_tokens = std::stoull(str(q["tokens"]));
    uint64_t tokens = curve_buy_tokens(e.curve, lamports, fee);
    // The SDK nets the fee off (amount - 1); the program's buy_exact_sol_in quote nets it first: within 1e-6.
    CHECK(std::fabs(double(tokens) - double(sdk_tokens)) <= double(sdk_tokens) * 1e-6 + 2);
    // Buying those tokens costs at most the budget, and within a lamport of the SDK's reverse quote.
    uint64_t cost = curve_buy_cost(e.curve, sdk_tokens, fee);
    uint64_t sdk_cost = std::stoull(str(q["cost"]));
    CHECK(cost + 2 >= sdk_cost && cost <= sdk_cost + 2);
    uint64_t proceeds = curve_sell_proceeds(e.curve, sdk_tokens, fee);
    uint64_t sdk_proceeds = std::stoull(str(q["proceeds"]));
    CHECK(proceeds + 2 >= sdk_proceeds && proceeds <= sdk_proceeds + 2);
  }
}

TEST(curve_overhang_drop_matches_replay) {
  // Same vector as research/test_replay_curve.py (overhang_drop_pct).
  Curve c;
  c.virtual_token = 289'900'000'000'000ULL;
  CHECK(std::fabs(overhang_drop_pct(c, 100'000'000'000'000ULL) - 44.71720904098031) < 1e-9);
  CHECK_EQ(overhang_drop_pct(c, 0), 0.0);
  c.virtual_token = 0;
  CHECK_EQ(overhang_drop_pct(c, 1), 0.0);
}

TEST(curve_buy_caps_at_remaining_tokens) {
  Curve c;
  c.virtual_token = 280'000'000'000'000ULL;
  c.virtual_sol = 114'000'000'000ULL;
  c.real_token = 1'000'000'000ULL;  // 1000 tokens left
  uint64_t tokens = curve_buy_tokens(c, 50'000'000, 125);
  CHECK_EQ(tokens, c.real_token);
  CHECK(curve_buy_cost(c, tokens, 125) < 50'000'000);
  c.complete = true;
  CHECK_EQ(curve_buy_tokens(c, 50'000'000, 125), uint64_t(0));
}
