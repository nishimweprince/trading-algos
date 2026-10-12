// Byte equality against @pump-fun/pump-swap-sdk + web3.js (tools/fixtures/gen.mjs).
#include "fixture.hpp"
#include "sol/amm.hpp"
#include "sol/constants.hpp"
#include "sol/layouts.hpp"
#include "sol/swap.hpp"
#include "test.hpp"

using namespace pd;
using namespace pdtest;

namespace {

struct Env {
  AmmConfig cfg;
  Pool pool;
  MintInfo mint;
  Keypair kp;
  UserKeys user;
  Pubkey mint_key, token_program, tip;
  Hash blockhash;
};

Env load_env() {
  auto& g = golden();
  Env e{};
  auto gc = b64bytes(g["accounts"]["globalConfig"]);
  auto fc = b64bytes(g["accounts"]["feeConfig"]);
  auto pl = b64bytes(g["accounts"]["pool"]);
  auto mn = b64bytes(g["accounts"]["mint"]);
  CHECK(decode_global_config(gc.data(), gc.size(), e.cfg));
  CHECK(decode_fee_config(fc.data(), fc.size(), e.cfg));
  CHECK(decode_pool(pl.data(), pl.size(), e.pool));
  e.token_program = key(g["accounts"]["mintOwner"]);
  CHECK(decode_mint(mn.data(), mn.size(), e.token_program == prog::TOKEN_2022, e.mint));
  auto seed = unhex(std::string_view(g["userSeedHex"]));
  e.kp = Keypair::from_seed(seed.data());
  derive_user_keys(e.kp.pubkey(), e.user);
  e.mint_key = key(g["seedMint"]);
  e.tip = key(g["tip"]);
  e.blockhash = key(g["blockhash"]);
  return e;
}

}  // namespace

TEST(golden_decoders_and_pdas) {
  Env e = load_env();
  auto& g = golden();
  CHECK_EQ(b58(e.kp.pubkey()), str(g["pdas"]["user"]));
  CHECK_EQ(b58(e.user.wsol_ata), str(g["pdas"]["userWsolAta"]));
  CHECK_EQ(b58(e.user.volume_acc), str(g["pdas"]["userVolumeAcc"]));
  CHECK_EQ(b58(derived().global_config), str(g["pdas"]["globalConfig"]));
  CHECK_EQ(b58(derived().fee_config), str(g["pdas"]["feeConfig"]));
  SwapKeys k;
  derive_swap_keys(e.user, e.mint_key, e.token_program, e.pool.coin_creator, false, false, k);
  CHECK_EQ(b58(k.pool), str(g["pdas"]["canonicalPool"]));
  CHECK_EQ(b58(k.pool), str(g["seedPool"]));
  CHECK_EQ(b58(k.pool_authority), str(g["pdas"]["poolAuthority"]));
  CHECK_EQ(b58(k.coin_creator_vault_auth), str(g["pdas"]["coinCreatorVaultAuthority"]));
  CHECK(k.pool_base_vault == e.pool.base_vault);
  CHECK(k.pool_quote_vault == e.pool.quote_vault);
  CHECK(e.pool.base_mint == e.mint_key);
  CHECK(e.pool.quote_mint == prog::WSOL);
  CHECK(e.pool.creator == k.pool_authority);
  CHECK_EQ(b58(e.pool.coin_creator), str(g["pool"]["coinCreator"]));
  CHECK_EQ(std::to_string(e.pool.virtual_quote), str(g["pool"]["virtualQuote"]));
  CHECK_EQ(e.pool.data_len, uint32_t(int64_t(g["pool"]["dataLen"])));
  CHECK(e.cfg.has_fee_config);
  CHECK(e.cfg.n_tiers > 0);
  CHECK(e.mint.supply > 0);
}

TEST(golden_pda_vectors) {
  for (auto v : golden()["pdaVectors"].get_array()) {
    Pubkey owner = key(v["owner"]), mint = key(v["mint"]);
    CHECK_EQ(b58(ata(owner, mint, prog::TOKEN)), str(v["ataToken"]));
    CHECK_EQ(b58(ata(owner, mint, prog::TOKEN_2022)), str(v["ata2022"]));
    CHECK_EQ(b58(pda({std::string_view("bonding-curve"), mint}, prog::PUMP_FUN)), str(v["bondingCurve"]));
    UserKeys u{};
    SwapKeys k;
    derive_swap_keys(u, mint, prog::TOKEN, owner, false, false, k);
    CHECK_EQ(b58(k.pool), str(v["canonicalPool"]));
  }
}

TEST(golden_on_curve_vectors) {
  int on = 0, off = 0;
  for (auto v : golden()["curveVectors"].get_array()) {
    auto b = unhex(std::string_view(v["bytes"]));
    bool expect = bool(v["onCurve"]);
    expect ? ++on : ++off;
    CHECK_EQ(is_on_curve(b.data()), expect);
  }
  CHECK(on > 0);
  CHECK(off > 0);
}

TEST(golden_swap_messages) {
  Env e = load_env();
  auto& g = golden();
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
    bool no_cc = false, cashback = false, mayhem = false;
    int64_t creator_fee = -1;
    (void)c["coinCreatorDefault"].get(no_cc);
    (void)c["cashback"].get(cashback);
    (void)c["mayhem"].get(mayhem);
    (void)c["creatorFeeBps"].get(creator_fee);
    uint64_t amount = std::stoull(str(c["amount"]));
    double slippage = double(int64_t(c["slippage"]));
    uint64_t base_res = std::stoull(str(c["baseReserve"]));
    uint64_t quote_res = std::stoull(str(c["quoteReserve"]));
    uint64_t virt = std::stoull(str(c["virtualQuote"]));

    Pubkey cc = no_cc ? Pubkey{} : e.pool.coin_creator;
    SwapKeys k;
    derive_swap_keys(e.user, e.mint_key, e.token_program, cc, cashback, mayhem, k);
    uint64_t pool_cf = creator_fee >= 0 ? uint64_t(creator_fee) : e.pool.creator_fee_bps;
    FeeBps fees = compute_fees_bps(e.cfg, true, e.mint.supply, base_res, quote_res + virt, mayhem, pool_cf);

    IxList l;
    if (!sell) {
      BuyQuote q = buy_quote_input(amount, slippage, base_res, quote_res, virt, fees, no_cc);
      CHECK(q.ok);
      buy_ixs(l, e.user, k, e.cfg, p, q.base_out, q.max_quote);
    } else {
      SellQuote q = sell_base_input(amount, slippage, base_res, quote_res, virt, fees, no_cc);
      CHECK(q.ok);
      sell_ixs(l, e.user, k, e.cfg, p, amount, q.min_quote, false);
    }
    // Instruction-level comparison first: a precise diagnostic when bytes differ.
    auto sdk_ixs = c["ixs"].get_array();
    size_t sdk_n = sdk_ixs.size();
    CHECK_EQ(size_t(l.n), sdk_n + 3);  // + CU limit, CU price, tip
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
    size_t len = compile_message_v0(e.user.user, l, e.blockhash, msg, sizeof(msg));
    CHECK(len > 0);
    std::string got = hex(msg, len);
    if (got != str(c["message"])) {
      std::fprintf(stderr, "  %s: message bytes differ (ours %zu bytes, sdk %zu)\n", name.c_str(), len,
                   str(c["message"]).size() / 2);
      ++failures();
    }
    SignedTx tx;
    CHECK(build_signed_tx(e.kp, l, e.blockhash, tx));
    CHECK(tx.len <= PACKET_DATA_SIZE);
  }
  CHECK(n >= 10);
}
