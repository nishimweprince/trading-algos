// Phase 1: every fixture account decodes, and decoded fields match independent decodes
// (spl-token for token accounts/mints, Anchor BorshAccountsCoder + the Whirlpool IDL, the
// Raydium pool-keys API) recorded by tools/fetch_fixtures.mjs.
#include <string>

#include "core/programs.hpp"
#include "fixture.hpp"
#include "state/accounts.hpp"
#include "test.hpp"

using namespace arb;
using namespace arbtest;

namespace {

simdjson::dom::element expected_root() {
  static simdjson::dom::parser p;
  static simdjson::padded_string ps = simdjson::padded_string(read_file(fixture_path("expected_decode.json")));
  static simdjson::dom::element root = p.parse(ps);
  return root;
}

std::string str(simdjson::dom::element e) {
  std::string_view s;
  return e.get(s) == simdjson::SUCCESS ? std::string(s) : std::string();
}
uint64_t num(simdjson::dom::element e) {
  uint64_t v = 0;
  if (e.get(v) == simdjson::SUCCESS) return v;
  int64_t i = 0;
  if (e.get(i) == simdjson::SUCCESS) return uint64_t(i);
  std::string_view s;
  if (e.get(s) == simdjson::SUCCESS) parse_u64(s, v);
  return v;
}
std::string u128s(unsigned __int128 v) { return arbtest::show(v); }
std::string i128s(__int128 v) { return arbtest::show(v); }

}  // namespace

TEST(decode_token_accounts_match_spl_token) {
  simdjson::dom::object exp = expected_root()["token_accounts"];
  int n = 0;
  for (auto kv : exp) {
    const FixtureAccount* f = fixture(std::string(kv.key));
    CHECK(f && f->exists);
    if (!f || !f->exists) continue;
    auto t = state::decode_token_account(f->raw.data.data(), f->raw.data.size(), f->raw.owner);
    CHECK(t.has_value());
    if (!t) continue;
    CHECK_EQ(b58(t->mint), str(kv.value["mint"]));
    CHECK_EQ(b58(t->owner), str(kv.value["owner"]));
    CHECK_EQ(t->amount, num(kv.value["amount"]));
    CHECK_EQ(uint64_t(t->state), num(kv.value["state"]));
    ++n;
  }
  CHECK(n >= 150);
}

TEST(decode_mints_match_spl_token) {
  simdjson::dom::object exp = expected_root()["mints"];
  // spl-token 0.4.15 ExtensionType names for the numbers this decoder reports.
  auto name = [](uint16_t t) -> std::string {
    switch (t) {
      case 1: return "TransferFeeConfig"; case 3: return "MintCloseAuthority"; case 4: return "ConfidentialTransferMint";
      case 6: return "DefaultAccountState"; case 9: return "NonTransferable"; case 10: return "InterestBearingConfig";
      case 12: return "PermanentDelegate"; case 14: return "TransferHook"; case 18: return "MetadataPointer";
      case 19: return "TokenMetadata"; case 20: return "GroupPointer"; case 21: return "TokenGroup";
      case 22: return "GroupMemberPointer"; case 23: return "TokenGroupMember"; case 25: return "ScaledUiAmountConfig";
      case 26: return "PausableConfig"; case 28: return "PermissionedBurn";
      default: return "";  // spl-token 0.4.15 has no name (it reports null)
    }
  };
  int n = 0, t22 = 0, with_fee = 0;
  for (auto kv : exp) {
    const FixtureAccount* f = fixture(std::string(kv.key));
    CHECK(f && f->exists);
    if (!f || !f->exists) continue;
    auto m = state::decode_mint(f->raw.data.data(), f->raw.data.size(), f->raw.owner);
    CHECK(m.has_value());
    if (!m) continue;
    CHECK_EQ(uint64_t(m->decimals), num(kv.value["decimals"]));
    CHECK_EQ(m->supply, num(kv.value["supply"]));
    std::string fa = str(kv.value["freeze_authority"]);
    CHECK_EQ(m->has_freeze_authority, !fa.empty());
    if (m->has_freeze_authority) CHECK_EQ(b58(m->freeze_authority), fa);
    CHECK_EQ(m->has_mint_authority, !str(kv.value["mint_authority"]).empty());
    simdjson::dom::array exts;
    if (kv.value["extensions"].get(exts) == simdjson::SUCCESS && exts.size() > 0) {
      ++t22;
      CHECK_EQ(size_t(m->n_ext), exts.size());
      size_t i = 0;
      for (auto e : exts) {
        if (i < m->n_ext && !e.is_null()) CHECK_EQ(name(m->ext_types[i]), str(e));
        ++i;
      }
    }
    simdjson::dom::element tf;
    if (kv.value["transfer_fee"].get(tf) == simdjson::SUCCESS) {
      ++with_fee;
      CHECK(m->has_transfer_fee);
      CHECK_EQ(m->older_fee.epoch, num(tf["older"]["epoch"]));
      CHECK_EQ(m->older_fee.maximum_fee, num(tf["older"]["max"]));
      CHECK_EQ(uint64_t(m->older_fee.basis_points), num(tf["older"]["bps"]));
      CHECK_EQ(m->newer_fee.epoch, num(tf["newer"]["epoch"]));
      CHECK_EQ(m->newer_fee.maximum_fee, num(tf["newer"]["max"]));
      CHECK_EQ(uint64_t(m->newer_fee.basis_points), num(tf["newer"]["bps"]));
    }
    ++n;
  }
  CHECK(n >= 50);
  CHECK(t22 >= 1);
  CHECK(with_fee >= 1);
}

TEST(decode_whirlpools_match_anchor_idl) {
  simdjson::dom::object exp = expected_root()["whirlpools"];
  int n = 0;
  for (auto kv : exp) {
    const FixtureAccount* f = fixture(std::string(kv.key));
    CHECK(f && f->exists);
    if (!f || !f->exists) continue;
    auto w = state::decode_whirlpool(f->raw.data.data(), f->raw.data.size(), f->raw.owner);
    CHECK(w.has_value());
    if (!w) continue;
    auto& e = kv.value;
    CHECK_EQ(b58(w->whirlpools_config), str(e["whirlpools_config"]));
    CHECK_EQ(uint64_t(w->tick_spacing), num(e["tick_spacing"]));
    CHECK_EQ(uint64_t(w->fee_tier_index), num(e["fee_tier_index"]));
    CHECK_EQ(uint64_t(w->fee_rate), num(e["fee_rate"]));
    CHECK_EQ(uint64_t(w->protocol_fee_rate), num(e["protocol_fee_rate"]));
    CHECK_EQ(u128s(w->liquidity), str(e["liquidity"]));
    CHECK_EQ(u128s(w->sqrt_price), str(e["sqrt_price"]));
    int64_t tci = 0;
    CHECK(e["tick_current_index"].get(tci) == simdjson::SUCCESS);
    CHECK_EQ(int64_t(w->tick_current_index), tci);
    CHECK_EQ(std::to_string(w->protocol_fee_owed_a), str(e["protocol_fee_owed_a"]));
    CHECK_EQ(std::to_string(w->protocol_fee_owed_b), str(e["protocol_fee_owed_b"]));
    CHECK_EQ(b58(w->token_mint_a), str(e["token_mint_a"]));
    CHECK_EQ(b58(w->token_vault_a), str(e["token_vault_a"]));
    CHECK_EQ(u128s(w->fee_growth_global_a), str(e["fee_growth_global_a"]));
    CHECK_EQ(b58(w->token_mint_b), str(e["token_mint_b"]));
    CHECK_EQ(b58(w->token_vault_b), str(e["token_vault_b"]));
    CHECK_EQ(u128s(w->fee_growth_global_b), str(e["fee_growth_global_b"]));
    ++n;
  }
  CHECK_EQ(n, 40);
  // Wrong owner, length and discriminator are rejected.
  const FixtureAccount* f = fixture(std::string(exp.begin().key()));
  auto bad_owner = state::decode_whirlpool(f->raw.data.data(), f->raw.data.size(), prog::RAYDIUM_V4);
  CHECK(!bad_owner && bad_owner.error().code == Errc::bad_owner);
  auto bad_len = state::decode_whirlpool(f->raw.data.data(), f->raw.data.size() - 1, prog::WHIRLPOOL);
  CHECK(!bad_len && bad_len.error().code == Errc::bad_length);
  std::vector<uint8_t> d = f->raw.data;
  d[0] ^= 1;
  auto bad_disc = state::decode_whirlpool(d.data(), d.size(), prog::WHIRLPOOL);
  CHECK(!bad_disc && bad_disc.error().code == Errc::bad_discriminator);
}

TEST(decode_tick_arrays_fixed_and_dynamic_match_idl) {
  simdjson::dom::object exp = expected_root()["tick_arrays"];
  int fixed = 0, dynamic = 0;
  for (auto kv : exp) {
    const FixtureAccount* f = fixture(std::string(kv.key));
    CHECK(f && f->exists);
    if (!f || !f->exists) continue;
    auto t = state::decode_tick_array(f->raw.data.data(), f->raw.data.size(), f->raw.owner);
    CHECK(t.has_value());
    if (!t) continue;
    bool is_dyn = str(kv.value["kind"]) == "dynamic";
    CHECK_EQ(t->dynamic, is_dyn);
    int64_t start = 0;
    CHECK(kv.value["start_tick_index"].get(start) == simdjson::SUCCESS);
    CHECK_EQ(int64_t(t->start_tick_index), start);
    CHECK_EQ(b58(t->whirlpool), str(kv.value["whirlpool"]));
    std::vector<int64_t> init;
    simdjson::dom::array ia = kv.value["initialized"];
    for (auto x : ia) init.push_back(int64_t(x));
    std::vector<std::string> nets;
    simdjson::dom::array na = kv.value["liquidity_net"];
    for (auto x : na) nets.push_back(str(x));
    size_t j = 0;
    for (int i = 0; i < state::TICK_ARRAY_SIZE; ++i) {
      bool want = j < init.size() && init[j] == i;
      CHECK_EQ(t->ticks[size_t(i)].initialized, want);
      if (want) {
        CHECK_EQ(i128s(t->ticks[size_t(i)].liquidity_net), nets[j]);
        ++j;
      }
    }
    if (is_dyn) ++dynamic; else ++fixed;
  }
  CHECK(fixed >= 10);
  CHECK(dynamic >= 10);
}

TEST(decode_tick_arrays_dynamic_consistent_with_pool) {
  // Dynamic arrays have no IDL-coder decode here; check structure instead: the whirlpool field
  // matches the pool whose PDA it is, and the sum of liquidity_net over a whole pool's arrays is
  // bounded by what an initialized position set can produce (each net is +/- a gross).
  simdjson::dom::object exp = expected_root()["tick_arrays"];
  int checked = 0;
  for (auto kv : exp) {
    if (str(kv.value["kind"]) != "dynamic") continue;
    const FixtureAccount* f = fixture(std::string(kv.key));
    auto t = state::decode_tick_array(f->raw.data.data(), f->raw.data.size(), f->raw.owner);
    CHECK(t.has_value());
    if (!t) continue;
    auto pda = state::tick_array_pda(t->whirlpool, t->start_tick_index);
    CHECK_EQ(b58(pda), std::string(kv.key));
    for (auto& tk : t->ticks) {
      if (!tk.initialized) continue;
      __int128 net = tk.liquidity_net < 0 ? -tk.liquidity_net : tk.liquidity_net;
      CHECK((unsigned __int128)net <= tk.liquidity_gross);
    }
    ++checked;
  }
  CHECK(checked >= 10);
}

TEST(decode_raydium_match_pool_keys_api) {
  simdjson::dom::object exp = expected_root()["raydium"];
  int n = 0;
  for (auto kv : exp) {
    const FixtureAccount* f = fixture(std::string(kv.key));
    CHECK(f && f->exists);
    if (!f || !f->exists) continue;
    auto r = state::decode_raydium_v4(f->raw.data.data(), f->raw.data.size(), f->raw.owner);
    CHECK(r.has_value());
    if (!r) continue;
    auto& e = kv.value;
    CHECK_EQ(b58(r->coin_vault), str(e["coin_vault"]));
    CHECK_EQ(b58(r->pc_vault), str(e["pc_vault"]));
    CHECK_EQ(b58(r->coin_mint), str(e["coin_mint"]));
    CHECK_EQ(b58(r->pc_mint), str(e["pc_mint"]));
    CHECK_EQ(b58(r->lp_mint), str(e["lp_mint"]));
    CHECK_EQ(b58(r->open_orders), str(e["open_orders"]));
    CHECK_EQ(b58(r->target_orders), str(e["target_orders"]));
    CHECK_EQ(b58(r->market), str(e["market"]));
    CHECK_EQ(b58(r->market_program), str(e["market_program"]));
    // Decimals cross-checked against the mint accounts; swap fee is 25/10000 on these pools.
    const FixtureAccount* cm = fixture(b58(r->coin_mint));
    const FixtureAccount* pm = fixture(b58(r->pc_mint));
    if (cm && cm->exists) CHECK_EQ(r->coin_decimals, uint64_t(state::decode_mint(cm->raw.data.data(), cm->raw.data.size(), cm->raw.owner)->decimals));
    if (pm && pm->exists) CHECK_EQ(r->pc_decimals, uint64_t(state::decode_mint(pm->raw.data.data(), pm->raw.data.size(), pm->raw.owner)->decimals));
    CHECK_EQ(r->swap_fee_numerator, uint64_t(25));
    CHECK_EQ(r->swap_fee_denominator, uint64_t(10000));
    ++n;
  }
  CHECK_EQ(n, 50);
}

TEST(decode_kamino_reserves_and_program_ids) {
  simdjson::dom::object exp = expected_root()["reserves"];
  int n = 0;
  for (auto kv : exp) {
    const FixtureAccount* f = fixture(std::string(kv.key));
    CHECK(f && f->exists);
    auto r = state::decode_kamino_reserve(f->raw.data.data(), f->raw.data.size(), f->raw.owner);
    CHECK(r.has_value());
    if (!r) continue;
    CHECK_EQ(b58(r->lending_market), str(kv.value["lending_market"]));
    CHECK_EQ(b58(r->liquidity_mint), str(kv.value["liquidity_mint"]));
    CHECK_EQ(b58(r->supply_vault), str(kv.value["supply_vault"]));
    CHECK_EQ(b58(r->fee_vault), str(kv.value["fee_vault"]));
    CHECK(r->token_program == prog::TOKEN);
    CHECK(r->mint_decimals == 9 || r->mint_decimals == 6);
    CHECK(r->flash_enabled());
    // The supply vault is a token account of the liquidity mint.
    const FixtureAccount* v = fixture(b58(r->supply_vault));
    if (v && v->exists) {
      auto t = state::decode_token_account(v->raw.data.data(), v->raw.data.size(), v->raw.owner);
      CHECK(t && t->mint == r->liquidity_mint);
    }
    ++n;
  }
  CHECK_EQ(n, 2);
}
