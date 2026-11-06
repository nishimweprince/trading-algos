// Phase 3: transaction serializer and instruction builders against web3.js 1.99 goldens
// (tests/golden_tx/golden.json from gen.mjs). Byte-identical messages and signed transactions required.
#include <simdjson.h>

#include <cstring>

#include "core/base58.hpp"
#include "core/base64.hpp"
#include "core/crypto.hpp"
#include "core/programs.hpp"
#include "fixture.hpp"
#include "flash/kamino.hpp"
#include "test.hpp"
#include "tx/builders.hpp"
#include "tx/dex_ix.hpp"

using namespace arb;
using namespace arbtest;

namespace {

struct Golden {
  simdjson::dom::parser parser;
  simdjson::padded_string body;
  simdjson::dom::element root;
  Golden() : body(read_file(std::string(ARB_GOLDEN_DIR) + "/golden.json")) { root = parser.parse(body); }
};

Golden& golden() {
  static Golden g;
  return g;
}

Pubkey pk(simdjson::dom::element e) { return parse_pubkey(std::string_view(e)); }

/** IxList straight from the JSON instructions (web3.js's own encoding). */
tx::IxList ixs_from_json(simdjson::dom::array arr) {
  tx::IxList l;
  for (auto ix : arr) {
    tx::Ix& x = l.add(pk(ix["program"]));
    for (auto a : simdjson::dom::array(ix["accounts"])) {
      simdjson::dom::array t = a;
      auto it = t.begin();
      Pubkey k = pk(*it);
      ++it;
      bool signer = bool(*it);
      ++it;
      bool writable = bool(*it);
      x.acc(k, signer, writable);
    }
    std::vector<uint8_t> d;
    b64_decode(std::string_view(ix["data"]), d);
    x.bytes(d.data(), d.size());
  }
  return l;
}

std::vector<tx::LookupTable> tables_from_json(simdjson::dom::array arr) {
  std::vector<tx::LookupTable> out;
  for (auto t : arr) {
    tx::LookupTable lt;
    lt.key = pk(t["key"]);
    for (auto a : simdjson::dom::array(t["addresses"])) lt.addresses.push_back(pk(a));
    out.push_back(lt);
  }
  return out;
}

bool same_ix(const tx::Ix& a, const tx::Ix& b) {
  if (a.program != b.program || a.n_accounts != b.n_accounts || a.data_len != b.data_len) return false;
  for (uint8_t i = 0; i < a.n_accounts; ++i)
    if (a.accounts[i].key != b.accounts[i].key || a.accounts[i].signer != b.accounts[i].signer ||
        a.accounts[i].writable != b.accounts[i].writable)
      return false;
  return std::memcmp(a.data, b.data, a.data_len) == 0;
}

Keypair golden_payer() {
  uint8_t seed[32];
  for (int i = 0; i < 32; ++i) seed[i] = uint8_t(i + 1);
  return Keypair::from_seed(seed);
}

}  // namespace

TEST(golden_tx_messages_and_signatures_byte_identical) {
  auto& g = golden();
  Keypair kp = golden_payer();
  CHECK(kp.pubkey() == pk(g.root["payer"]));
  Hash bh = pk(g.root["blockhash"]);
  Pubkey tip = pk(g.root["tip_account"]);
  size_t n = 0;
  for (auto c : simdjson::dom::array(g.root["cases"])) {
    std::string name{std::string_view(c["name"])};
    tx::IxList l = ixs_from_json(c["instructions"]);
    auto tables = tables_from_json(c["tables"]);
    std::vector<uint8_t> want_msg, want_tx;
    b64_decode(std::string_view(c["message"]), want_msg);
    b64_decode(std::string_view(c["tx"]), want_tx);
    uint8_t msg[2048];
    Pubkey statics[1] = {tip};
    auto m = tx::compile_message_v0(kp.pubkey(), l, bh, tables, msg, sizeof msg, statics);
    CHECK(m.has_value());
    if (!m) continue;
    bool same = *m == want_msg.size() && std::memcmp(msg, want_msg.data(), *m) == 0;
    if (!same) std::fprintf(stderr, "    %s: message differs (%zu vs %zu bytes)\n", name.c_str(), *m, want_msg.size());
    CHECK(same);
    tx::SignedTx st;
    auto t = tx::build_tx(kp.pubkey(), &kp, l, bh, tables, st, statics);
    CHECK(t.has_value());
    CHECK(st.len == want_tx.size() && std::memcmp(st.bytes, want_tx.data(), st.len) == 0);
    ++n;
  }
  CHECK_EQ(n, size_t(4));
}

TEST(golden_builders_encode_like_web3) {
  auto& g = golden();
  Keypair kp = golden_payer();
  Pubkey payer = kp.pubkey();
  // Kamino reserve record from the golden's synthetic accounts; the market authority PDA is derived here.
  state::KaminoReserve kr;
  kr.lending_market = pk(g.root["reserve"]["market"]);
  kr.liquidity_mint = pk(g.root["reserve"]["mint"]);
  kr.supply_vault = pk(g.root["reserve"]["supply"]);
  kr.fee_vault = pk(g.root["reserve"]["fee"]);
  kr.token_program = prog::TOKEN;
  kr.flash_loan_fee_sf = 0;
  flash::FlashReserve fr = flash::make_reserve(pk(g.root["reserve"]["reserve"]), kr, 1);
  CHECK(fr.lending_market_authority == pk(g.root["lma"]));
  Pubkey user_ata = pk(g.root["user_ata"]);
  Pubkey tip = pk(g.root["tip_account"]);
  uint64_t amount = std::stoull(std::string(std::string_view(g.root["amount"])));
  for (auto c : simdjson::dom::array(g.root["cases"])) {
    std::string name{std::string_view(c["name"])};
    tx::IxList want = ixs_from_json(c["instructions"]);
    tx::IxList got;
    if (name == "alt_create_extend") {
      Pubkey table;
      tx::create_lookup_table(got, payer, payer, 271'828'182, &table);
      CHECK(table == pk(g.root["alt_table"]));
      std::vector<Pubkey> addrs;
      for (int i = 0; i < 20; ++i) {
        Pubkey k{};
        k[0] = uint8_t(100 + i);
        k[1] = 0x42;
        k[31] = 0x99;
        addrs.push_back(k);
      }
      tx::extend_lookup_table(got, table, payer, payer, addrs);
    } else {
      // [CU limit][CU price?][borrow][execute (from the golden: its account list needs pool snapshots)][repay][tip]
      tx::set_compute_unit_limit(got, 412'345);
      bool priced = want.n == 6;
      if (priced) tx::set_compute_unit_price(got, 7'777);
      uint8_t bi = got.n;
      flash::borrow_ix(got, fr, payer, user_ata, amount);
      got.ixs[got.n++] = want.ixs[bi + 1];
      flash::repay_ix(got, fr, payer, user_ata, amount, bi);
      tx::system_transfer(got, payer, tip, 1'234'567);
    }
    CHECK_EQ(got.n, want.n);
    for (uint8_t i = 0; i < got.n && i < want.n; ++i) {
      bool ok = same_ix(got.ixs[i], want.ixs[i]);
      if (!ok) std::fprintf(stderr, "    %s: instruction %u differs\n", name.c_str(), unsigned(i));
      CHECK(ok);
    }
  }
}

TEST(tip_account_stays_static_and_size_limit) {
  auto& g = golden();
  Keypair kp = golden_payer();
  Hash bh = pk(g.root["blockhash"]);
  Pubkey tip = pk(g.root["tip_account"]);
  // A table that also holds the tip account: with force_static the tip stays in the static keys.
  tx::IxList l;
  tx::system_transfer(l, kp.pubkey(), tip, 5000);
  tx::LookupTable t{Pubkey{9}, {tip}};
  uint8_t msg[512];
  tx::CompileStats st;
  Pubkey statics[1] = {tip};
  auto m = tx::compile_message_v0(kp.pubkey(), l, bh, std::span<const tx::LookupTable>(&t, 1), msg, sizeof msg, statics, &st);
  CHECK(m.has_value() && st.lookup_writable == 0 && st.tables_used == 0);
  auto m2 = tx::compile_message_v0(kp.pubkey(), l, bh, std::span<const tx::LookupTable>(&t, 1), msg, sizeof msg, {}, &st);
  CHECK(m2.has_value() && st.lookup_writable == 1);
  // Without tables, the full 18-leg-account route is over 1,232 bytes: tx_too_large, never a truncated tx.
  tx::IxList big;
  tx::set_compute_unit_limit(big, 1);
  tx::Ix& ix = big.add(Pubkey{7});
  for (int i = 0; i < 40; ++i) {
    Pubkey k{};
    k[0] = uint8_t(i + 1);
    k[5] = 3;
    ix.rw(k);
  }
  tx::SignedTx out;
  auto r = tx::build_tx(kp.pubkey(), &kp, big, bh, {}, out);
  CHECK(!r.has_value() && r.error().code == Errc::tx_too_large);
}

TEST(lookup_table_account_decode) {
  // LookupTableMeta: u32 state=1 | deactivation u64 | last_extended u64 | start u8 | Option<Pubkey> | u16 pad.
  std::vector<uint8_t> d(56 + 64, 0);
  uint32_t one = 1;
  uint64_t deact = UINT64_MAX, last = 777;
  std::memcpy(d.data(), &one, 4);
  std::memcpy(d.data() + 4, &deact, 8);
  std::memcpy(d.data() + 12, &last, 8);
  d[20] = 1;
  d[21] = 1;
  d[22] = 0xAA;
  d[56] = 0x11;
  d[88] = 0x22;
  auto a = tx::decode_lookup_table(d.data(), d.size(), prog::ALT);
  CHECK(a.has_value());
  CHECK(a->last_extended_slot == 777 && a->last_extended_start == 1 && a->has_authority && a->authority[0] == 0xAA);
  CHECK(a->addresses.size() == 2 && a->addresses[0][0] == 0x11 && a->addresses[1][0] == 0x22);
  CHECK(!tx::decode_lookup_table(d.data(), d.size(), prog::SYSTEM).has_value());
  CHECK(!tx::decode_lookup_table(d.data(), 57, prog::ALT).has_value());
}

TEST(cp_swap_goldens_both_directions_from_upstream_idls) {
  simdjson::dom::parser parser;
  simdjson::padded_string body(read_file(std::string(ARB_GOLDEN_DIR) + "/cp_swaps.json"));
  auto root = parser.parse(body).value();
  const auto in = root["inputs"];
  auto key = [&](const char* n) { return pk(in[n]); };
  CHECK(prog::RAYDIUM_CPMM == pk(root["programs"]["cpmm"]));
  CHECK(prog::PUMPSWAP == pk(root["programs"]["pumpswap"]));
  CHECK(prog::PUMP_FEES == pk(root["programs"]["pump_fees"]));
  size_t count = 0;
  for (auto c : simdjson::dom::array(root["cases"])) {
    PoolState p;
    p.kind = std::string_view(c["dex"]) == "cpmm" ? DexKind::Cpmm : DexKind::PumpSwap;
    p.address = key("pool"); p.mint_a = key("mint_a"); p.mint_b = key("mint_b");
    p.vault_a = key("vault_a"); p.vault_b = key("vault_b");
    p.cpmm.amm_config = key("config"); p.cpmm.observation_key = key("observation");
    p.cpmm.token_0_program = key("token_a"); p.cpmm.token_1_program = key("token_b");
    auto m = std::make_shared<state::Mint>(); m->token_program = key("token_a"); p.mint_a_info = m;
    m = std::make_shared<state::Mint>(); m->token_program = key("token_b"); p.mint_b_info = m;
    p.pump_cfg.buyback_fee_recipients[0] = key("recipient");
    const bool dir = bool(c["a_to_b"]);
    tx::dexix::UserAccounts user{key("user"), key("user_a"), key("user_b")};
    const auto got = tx::dexix::swap_accounts(p, dir, user);
    simdjson::dom::array expected = c["accounts"];
    CHECK_EQ(got.size(), expected.size());
    size_t i = 0;
    for (auto e : expected) {
      if (i >= got.size()) break;
      CHECK(got[i].key == pk(e["key"]));
      CHECK_EQ(got[i].signer, bool(e["signer"])); CHECK_EQ(got[i].writable, bool(e["writable"])); ++i;
    }
    CHECK(tx::dexix::program_of(p.kind) == pk(c["program"]));
    std::vector<uint8_t> bytes;
    CHECK(b64_decode(std::string_view(c["data"]), bytes));
    CHECK(tx::dexix::swap_data(p.kind, dir, 123456789, 98765) == bytes);
    tx::IxList list; tx::dexix::swap(list, p, dir, user, 123456789, 98765);
    CHECK_EQ(list.n, uint8_t(1)); CHECK(list.ixs[0].program == pk(c["program"]));
    CHECK_EQ(list.ixs[0].n_accounts, got.size());
    ++count;
  }
  CHECK_EQ(count, size_t(4));
}
