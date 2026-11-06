#include <algorithm>
#include <cstring>
#include <stdexcept>

#include "core/config.hpp"
#include "dex/clmm_math.hpp"
#include "dex/quoter.hpp"
#include "fixture.hpp"
#include "test.hpp"
#include "tx/dex_ix.hpp"

using namespace arb;
using namespace arbtest;
namespace {
struct Captured {
  simdjson::dom::parser parser;
  simdjson::padded_string body{read_file(fixture_path("clmm_accounts.json"))};
  simdjson::dom::element root = parser.parse(body);
};
std::string text(simdjson::dom::element e) { return std::string(std::string_view(e)); }
uint64_t word(simdjson::dom::element e) {
  uint64_t n = 0;
  if (e.get(n) == simdjson::SUCCESS) return n;
  CHECK(parse_u64(text(e), n));
  return n;
}
u128 wide(simdjson::dom::element e) {
  u128 n = 0;
  for (char c : text(e)) { CHECK(c >= '0' && c <= '9'); n = n * 10 + unsigned(c - '0'); }
  return n;
}
int32_t integer(simdjson::dom::element e) { return int32_t(int64_t(e)); }
state::RawAccount raw(std::string_view key, simdjson::dom::element e) {
  state::RawAccount a;
  CHECK(b58_decode32(key, a.key));
  CHECK(b58_decode32(text(e["owner"]), a.owner));
  CHECK(b64_decode(text(e["data"]), a.data));
  return a;
}
PoolState model() {
  PoolState p;
  p.kind = DexKind::Clmm;
  p.address = prog::RAYDIUM_CLMM;
  p.complete = true;
  p.clmm.tick_spacing = 1;
  p.clmm.liquidity = 1'000'000'000'000ULL;
  p.clmm.sqrt_price_x64 = *dex::clmm::sqrt_price_at_tick(0);
  p.clmm_cfg.tick_spacing = 1;
  p.clmm_cfg.trade_fee_rate = 3000;
  auto mint = std::make_shared<state::Mint>();
  mint->token_program = prog::TOKEN;
  p.mint_a_info = mint; p.mint_b_info = mint;
  return p;
}
void set_bit(state::ClmmPool& pool, state::ClmmBitmapExtension& ext, int32_t index) {
  uint64_t* words;
  int bit;
  if (index >= -512 && index < 512) { words = pool.tick_array_bitmap; bit = index + 512; }
  else {
    int block = index >= 512 ? index / 512 - 1 : (-index - 1) / 512 - 1;
    words = index >= 512 ? ext.positive[block] : ext.negative[block];
    bit = (index % 512 + 512) % 512;
  }
  words[bit / 64] |= uint64_t(1) << (bit % 64);
}
void add_tick(PoolState& p, int32_t start, int32_t tick) {
  auto a = std::make_shared<state::ClmmTickArray>();
  a->pool = p.address; a->start_tick_index = start;
  auto& t = a->ticks[size_t((tick - start) / p.clmm.tick_spacing)];
  t.tick = tick; t.liquidity_gross = p.clmm.liquidity;
  auto& slot = p.ticks[p.n_ticks++];
  slot.start = start; slot.state = TickArraySlot::Loaded; slot.clmm = a;
  slot.key = state::clmm_tick_array_pda(p.address, start);
}
bool throws_accounts(const PoolState& p) {
  try { (void)tx::dexix::swap_accounts(p, false, {}); return false; }
  catch (const std::invalid_argument&) { return true; }
}
}

TEST(clmm_real_accounts_match_pinned_anchor_idl) {
  Captured f;
  int pools = 0, configs = 0, arrays = 0, bitmaps = 0, extended_arrays = 0;
  for (auto kv : simdjson::dom::object(f.root["accounts"])) {
    auto a = raw(kv.key, kv.value);
    auto e = kv.value["expected"];
    auto type = text(kv.value["type"]);
    if (type == "PoolState") {
      auto p = state::decode_clmm_pool(a.data.data(), a.data.size(), a.owner); CHECK(p); if (!p) continue;
      ++pools;
      CHECK_EQ(b58(p->amm_config), text(e["amm_config"]));
      CHECK_EQ(b58(p->token_mint_0), text(e["token_mint_0"]));
      CHECK_EQ(b58(p->token_mint_1), text(e["token_mint_1"]));
      CHECK_EQ(b58(p->token_vault_0), text(e["token_vault_0"]));
      CHECK_EQ(b58(p->token_vault_1), text(e["token_vault_1"]));
      CHECK_EQ(b58(p->observation_key), text(e["observation_key"]));
      CHECK_EQ(p->liquidity, wide(e["liquidity"]));
      CHECK_EQ(p->sqrt_price_x64, wide(e["sqrt_price_x64"]));
      CHECK_EQ(p->tick_current, integer(e["tick_current"]));
      CHECK_EQ(p->tick_spacing, word(e["tick_spacing"]));
      CHECK_EQ(p->status, word(e["status"])); CHECK_EQ(p->fee_on, word(e["fee_on"]));
      CHECK_EQ(p->open_time, word(e["open_time"]));
      bool dynamic = false;
      for (auto field : simdjson::dom::object(e["dynamic_fee_info"])) {
        // reserved padding is ignored, exactly as the program's fee-info presence check.
        if (field.key == "padding") continue;
        if (word(field.value)) dynamic = true;
      }
      CHECK_EQ(p->dynamic_fee, dynamic);
      for (size_t i = 0; i < 16; ++i) CHECK_EQ(p->tick_array_bitmap[i], word(e["tick_array_bitmap"].at(i)));
    } else if (type == "AmmConfig") {
      auto c = state::decode_clmm_config(a.data.data(), a.data.size(), a.owner); CHECK(c); if (!c) continue;
      ++configs;
      CHECK_EQ(c->tick_spacing, word(e["tick_spacing"]));
      CHECK_EQ(c->trade_fee_rate, word(e["trade_fee_rate"]));
      CHECK_EQ(c->protocol_fee_rate, word(e["protocol_fee_rate"]));
      CHECK_EQ(c->fund_fee_rate, word(e["fund_fee_rate"]));
    } else if (type == "TickArrayState") {
      auto t = state::decode_clmm_tick_array(a.data.data(), a.data.size(), a.owner); CHECK(t); if (!t) continue;
      ++arrays;
      CHECK_EQ(b58(t->pool), text(e["pool_id"]));
      CHECK_EQ(t->start_tick_index, integer(e["start_tick_index"]));
      CHECK(state::clmm_tick_array_pda(t->pool, t->start_tick_index) == a.key);
      if (t->start_tick_index < -30720 || t->start_tick_index >= 30720) ++extended_arrays;
      for (size_t i = 0; i < 60; ++i) {
        auto x = e["ticks"].at(i);
        CHECK_EQ(t->ticks[i].tick, integer(x["tick"]));
        CHECK_EQ(show(t->ticks[i].liquidity_net), text(x["liquidity_net"]));
        CHECK_EQ(t->ticks[i].liquidity_gross, wide(x["liquidity_gross"]));
        CHECK_EQ(t->ticks[i].limit_orders, word(x["orders_amount"]) != 0 || word(x["part_filled_orders_remaining"]) != 0);
      }
    } else if (type == "TickArrayBitmapExtension") {
      auto b = state::decode_clmm_bitmap_extension(a.data.data(), a.data.size(), a.owner); CHECK(b); if (!b) continue;
      ++bitmaps;
      CHECK_EQ(b58(b->pool), text(e["pool_id"]));
      CHECK(state::clmm_bitmap_extension_pda(b->pool) == a.key);
      for (size_t i = 0; i < 14; ++i) for (size_t j = 0; j < 8; ++j) {
        CHECK_EQ(b->positive[i][j], word(e["positive_tick_array_bitmap"].at(i).at(j)));
        CHECK_EQ(b->negative[i][j], word(e["negative_tick_array_bitmap"].at(i).at(j)));
      }
      CHECK(!state::decode_clmm_bitmap_extension(a.data.data(), a.data.size(), prog::TOKEN));
      CHECK(!state::decode_clmm_bitmap_extension(a.data.data(), a.data.size()-1, a.owner));
      a.data[0] ^= 1;
      CHECK(!state::decode_clmm_bitmap_extension(a.data.data(), a.data.size(), a.owner));
    }
  }
  CHECK_EQ(pools, 2); CHECK_EQ(configs, 2); CHECK_EQ(bitmaps, 2); CHECK_EQ(arrays, 8); CHECK_EQ(extended_arrays, 2);
}

TEST(clmm_bitmap_traversal_matches_sparse_sorted_oracle_both_directions) {
  // Exhaustively query all legal spacing-1 array starts against a separately sorted list.
  state::ClmmPool p; p.tick_spacing = 1;
  state::ClmmBitmapExtension ext;
  const std::vector<int32_t> indices{-7394,-7169,-7168,-1537,-1536,-1025,-1024,-513,-512,-511,-1,0,511,512,513,1023,1024,7168,7393};
  for (auto i : indices) set_bit(p, ext, i);
  for (int32_t i = -7394; i <= 7393; ++i) for (bool zfo : {true, false}) for (bool inclusive : {true, false}) {
    auto actual = dex::clmm::initialized_array(p, &ext, i*60, zfo, inclusive);
    auto expected = indices.end();
    if (zfo) {
      for (auto it = indices.begin(); it != indices.end(); ++it) if (*it < i || (inclusive && *it == i)) expected = it;
    } else expected = std::find_if(indices.begin(), indices.end(), [&](int x) { return x > i || (inclusive && x == i); });
    CHECK_EQ(bool(actual), expected != indices.end());
    if (actual && expected != indices.end()) CHECK_EQ(*actual, *expected * 60);
  }
  p.tick_spacing = 0;
  CHECK(!dex::clmm::initialized_array(p, &ext, 0, false));
  p.tick_spacing = 1;
  CHECK(!dex::clmm::initialized_array(p, &ext, 1, false));
  int32_t start = 0;
  // Regression: abs(compressed) used to alias far-negative extension bits into the default bitmap.
  p.tick_array_bitmap[0] = 2;
  CHECK(!dex::clmm::current_array_initialized(p.tick_array_bitmap, -513*60, 1, start));
}

TEST(clmm_unknown_bitmap_is_gap_not_empty_liquidity) {
  state::ClmmPool p; p.tick_spacing = 1;
  auto gap = dex::clmm::initialized_array(p, nullptr, 511*60, false);
  CHECK(!gap); CHECK(gap.error().code == Errc::not_quotable);
  state::ClmmBitmapExtension ext;
  auto empty = dex::clmm::initialized_array(p, &ext, 511*60, false);
  CHECK(!empty); CHECK(empty.error().code == Errc::insufficient_liquidity);
  p.tick_spacing = 60;
  auto bounded = dex::clmm::initialized_array(p, nullptr, 0, true);
  CHECK(!bounded); CHECK(bounded.error().code == Errc::insufficient_liquidity);
}

TEST(clmm_quote_crosses_default_into_extension_and_builds_directional_accounts) {
  auto p = model();
  p.clmm.tick_current = 30719;
  p.clmm.sqrt_price_x64 = *dex::clmm::sqrt_price_at_tick(p.clmm.tick_current);
  auto ext = std::make_shared<state::ClmmBitmapExtension>(); ext->pool = p.address;
  set_bit(p.clmm, *ext, 511); set_bit(p.clmm, *ext, 512); set_bit(p.clmm, *ext, 513);
  p.clmm_bitmap = ext;
  add_tick(p, 30660, 30719); add_tick(p, 30720, 30720); add_tick(p, 30780, 30800);
  auto first_step = dex::clmm::compute_swap_exact_in(p.clmm.sqrt_price_x64, *dex::clmm::sqrt_price_at_tick(30720),
      p.clmm.liquidity, 1'000'000'000'000ULL, p.clmm_cfg.trade_fee_rate, false, true);
  CHECK(first_step); if (!first_step) return;
  uint64_t amount = first_step->amount_in + first_step->fee_amount + 10000;
  dex::ClmmQuoter q;
  auto result = q.quote_exact_in(p, false, amount);
  CHECK(result); if (!result) return;
  CHECK_EQ(result->amount_in, amount); CHECK(result->amount_out > 0); CHECK(!result->crosses_boundary);
  auto accounts = tx::dexix::swap_accounts(p, false, {});
  CHECK_EQ(accounts.size(), size_t(17));
  CHECK(accounts[13].key == state::clmm_bitmap_extension_pda(p.address)); CHECK(accounts[13].writable);
  CHECK(accounts[14].key == p.ticks[0].key); CHECK(accounts[15].key == p.ticks[1].key); CHECK(accounts[16].key == p.ticks[2].key);
  p.clmm_bitmap.reset();
  auto gap = q.quote_exact_in(p, false, amount);
  CHECK(gap); CHECK(gap->cache_gap); CHECK(gap->crosses_boundary);
  p.clmm_bitmap = ext; p.ticks[1].state = TickArraySlot::Missing;
  gap = q.quote_exact_in(p, false, amount); CHECK(gap); CHECK(gap->cache_gap);
}

TEST(clmm_strict_token2022_extension_refusal_in_quote_and_tx) {
  auto p = model();
  dex::ClmmQuoter q;
  auto m = std::make_shared<state::Mint>(); m->token_program = prog::TOKEN_2022;
  p.mint_a_info = m;
  CHECK(q.supports(p));
  for (uint16_t type : {1,3,4,6,9,10,12,14,16,18,19,25,26,63,64,65535}) {
    m->n_ext = 1; m->ext_types[0] = type; m->ext_mask = type < 64 ? uint64_t(1) << type : 0;
    CHECK(!q.supports(p)); CHECK(throws_accounts(p));
    auto result = q.quote_exact_in(p, true, 1000); CHECK(!result); CHECK(result.error().code == Errc::unsupported);
  }
  m->n_ext = 0; m->ext_mask = 0; m->has_transfer_fee = true; // even when both fee rates are zero
  CHECK(!q.supports(p)); CHECK(throws_accounts(p));
  p.mint_a_info.reset(); CHECK(!q.supports(p)); CHECK(throws_accounts(p));
  CHECK(!Config{}.dex.clmm_enabled); CHECK(!Config{}.discovery.track_clmm);
}

TEST(clmm_tx_refuses_missing_first_array_and_wrong_pool_bitmap) {
  auto p = model();
  auto b = std::make_shared<state::ClmmBitmapExtension>(); b->pool = p.address;
  set_bit(p.clmm, *b, 0); p.clmm_bitmap = b;
  CHECK(throws_accounts(p));
  add_tick(p, 0, 1); CHECK(!throws_accounts(p));
  b->pool = prog::TOKEN; CHECK(throws_accounts(p));
  b->pool = p.address; p.ticks[0].key = prog::TOKEN; CHECK(throws_accounts(p));
}

namespace {
struct World {
  std::vector<state::RawAccount> pools;
  state::AccountLookup accounts;
  state::DiscoveryConfig cfg;
  World() {
    Captured f;
    cfg.base_mints = {mint::WSOL, mint::USDC}; cfg.min_pool_tvl_sol = 0;
    for (auto kv : simdjson::dom::object(f.root["accounts"])) {
      auto a = raw(kv.key, kv.value);
      if (text(kv.value["type"]) == "PoolState") pools.push_back(a);
      accounts[a.key] = a;
    }
    for (auto& a : pools) {
      auto p = *state::decode_clmm_pool(a.data.data(), a.data.size(), a.owner);
      // Vaults and mints below are synthetic; real CLMM pool/config/bitmap/tick bytes are unchanged.
      for (auto [m,v] : {std::pair{p.token_mint_0,p.token_vault_0},std::pair{p.token_mint_1,p.token_vault_1}}) {
        state::RawAccount ma{m,prog::TOKEN,std::vector<uint8_t>(state::MINT_LEN)};
        ma.data[44] = 9; ma.data[45] = 1; accounts[m] = ma;
        state::RawAccount va{v,prog::TOKEN,std::vector<uint8_t>(state::TOKEN_ACCOUNT_LEN)};
        std::memcpy(va.data.data(),m.data(),32); va.data[108] = 1;
        wr_u64(va.data.data()+64,100'000'000'000ULL); accounts[v] = va;
      }
    }
  }
};
bool apply(StateStore& s, const state::RawAccount& a, uint64_t slot, bool resync = false) {
  AccountUpdate u; u.key=a.key; u.owner=a.owner; u.data=a.data.data(); u.len=a.data.size(); u.slot=slot; u.from_resync=resync;
  return s.apply(u);
}
}

TEST(clmm_discovery_reads_extension_and_refuses_all_mint_extensions) {
  World w;
  auto keys = state::dependent_keys(w.pools,w.cfg);
  for (auto& p : w.pools) CHECK(std::find(keys.begin(),keys.end(),state::clmm_bitmap_extension_pda(p.key)) != keys.end());
  auto found = state::discover(w.pools,w.accounts,w.cfg);
  CHECK_EQ(found.tracked.size(),size_t(2));
  for (auto& c : found.tracked) CHECK(c.def.tick_arrays.size() <= MAX_TICK_ARRAYS);
  auto key = state::clmm_bitmap_extension_pda(w.pools.front().key);
  auto bitmap = w.accounts.at(key);
  w.accounts.erase(key);
  found = state::discover(w.pools,w.accounts,w.cfg);
  CHECK_EQ(found.reject_counts["clmm_bitmap_missing"],size_t(1));
  w.accounts[key] = bitmap; w.accounts[key].data[8] ^= 1;
  found = state::discover(w.pools,w.accounts,w.cfg);
  CHECK_EQ(found.reject_counts["clmm_bitmap_invalid"],size_t(1));
  w.accounts[key] = bitmap;
  auto d = *state::pool_def_from_account(w.pools.front(),w.cfg);
  auto& m = w.accounts.at(d.mint_a); m.owner=prog::TOKEN_2022;
  for (uint16_t type : {1,14,18,64,65535}) {
    m.data.resize(170 + (type == 1 ? 108 : 0)); m.data[165]=1;
    wr_u16(m.data.data()+166,type); wr_u16(m.data.data()+168,type == 1 ? 108 : 0);
    found = state::discover(w.pools,w.accounts,w.cfg);
    CHECK_EQ(found.tracked.size(),size_t(0)); // both pools share this mint
    CHECK_EQ(found.reject_counts["clmm_mint_extensions"],size_t(2));
  }
}

TEST(clmm_discovery_selects_initialized_arrays_outside_default_bitmap) {
  World w;
  auto& a = w.pools.front();
  auto p = *state::decode_clmm_pool(a.data.data(),a.data.size(),a.owner);
  // Move the synthetic price to the last default array; real extension bits stay unchanged.
  wr_i32(a.data.data()+269,-30721);
  wr_u128(a.data.data()+253,*dex::clmm::sqrt_price_at_tick(-30721));
  auto found=state::discover(w.pools,w.accounts,w.cfg);
  CHECK_EQ(found.tracked.size(),size_t(2));
  auto c = std::find_if(found.tracked.begin(),found.tracked.end(),[&](auto& c){return c.def.address == a.key;});
  CHECK(c != found.tracked.end()); if(c == found.tracked.end()) return;
  CHECK_EQ(c->def.tick_arrays.front().first,-30780);
  auto braw=w.accounts.at(state::clmm_bitmap_extension_pda(a.key));
  auto b=*state::decode_clmm_bitmap_extension(braw.data.data(),braw.data.size(),braw.owner);
  auto expected=dex::clmm::initialized_array(p,&b,-30780,true);
  CHECK(expected);
  CHECK(std::any_of(c->def.tick_arrays.begin(),c->def.tick_arrays.end(),[&](auto& t){return t.first == *expected;}));
}

TEST(clmm_state_subscribes_validates_invalidates_and_resyncs_bitmap) {
  World w;
  auto found=state::discover(w.pools,w.accounts,w.cfg);
  CHECK(!found.tracked.empty()); if(found.tracked.empty()) return;
  auto def=found.tracked.front().def;
  StateStore store;
  auto id=store.add_pool(def);
  auto keys=store.subscribed_keys();
  auto bitmap_key=state::clmm_bitmap_extension_pda(def.address);
  CHECK(std::find(keys.begin(),keys.end(),bitmap_key) != keys.end());
  for (auto& key : keys) {
    auto a=w.accounts.find(key);
    if(a != w.accounts.end()) CHECK(apply(store,a->second,100));
  }
  CHECK(store.pool(id)->complete); CHECK(store.pool(id)->clmm_bitmap); CHECK(store.pool(id)->quotable(5));
  auto bitmap=w.accounts.at(bitmap_key);
  CHECK(apply(store,bitmap,101,true)); CHECK(store.pool(id)->awaiting_push);
  CHECK(apply(store,bitmap,102)); CHECK(!store.pool(id)->awaiting_push);
  bitmap.data[8] ^= 1;
  CHECK(!apply(store,bitmap,103)); CHECK(!store.pool(id)->clmm_bitmap);
  bitmap=w.accounts.at(bitmap_key);
  CHECK(apply(store,bitmap,102)); CHECK(!store.pool(id)->clmm_bitmap); // malformed newer update fences stale recovery
  CHECK(apply(store,bitmap,104)); CHECK(store.pool(id)->clmm_bitmap);
  AccountUpdate missing; missing.key=bitmap_key; missing.slot=105; missing.missing=true;
  CHECK(store.apply(missing)); CHECK(!store.pool(id)->clmm_bitmap);
  CHECK(apply(store,bitmap,104)); CHECK(!store.pool(id)->clmm_bitmap); // stale cannot revive
  CHECK(apply(store,bitmap,106)); CHECK(store.pool(id)->clmm_bitmap);
  store.mark_suspect(bitmap_key); CHECK(store.pool(id)->awaiting_push);
  CHECK(apply(store,bitmap,107)); CHECK(!store.pool(id)->awaiting_push);
  auto config=w.accounts.at(def.config); wr_u16(config.data.data()+51,2);
  CHECK(apply(store,config,108)); CHECK(!store.pool(id)->complete);
  config=w.accounts.at(def.config); CHECK(apply(store,config,109)); CHECK(store.pool(id)->complete);
  AccountUpdate missing_mint; missing_mint.key=def.mint_a; missing_mint.slot=110; missing_mint.missing=true;
  CHECK(store.apply(missing_mint)); CHECK(!store.pool(id)->complete); CHECK(!store.pool(id)->mint_a_info);
}

TEST(clmm_swap_v2_fixed_accounts_and_data_match_pinned_idl) {
  simdjson::dom::parser parser;
  simdjson::padded_string bytes(read_file(std::string(ARB_SOURCE_DIR)+"/harness/idl/raydium_clmm.json"));
  auto root=parser.parse(bytes);
  auto p=model();
  p.mint_a=mint::WSOL; p.mint_b=mint::USDC; p.vault_a=prog::TOKEN; p.vault_b=prog::TOKEN_2022;
  p.clmm.amm_config=prog::MEMO; p.clmm.observation_key=prog::SYSTEM;
  p.clmm.tick_array_bitmap[8]=1; add_tick(p,0,1);
  tx::dexix::UserAccounts u{prog::KLEND,prog::WHIRLPOOL,prog::RAYDIUM_V4};
  for(auto ix : simdjson::dom::array(root["instructions"])) {
    if(text(ix["name"]) != "swap_v2") continue;
    auto data=tx::dexix::swap_data(DexKind::Clmm,true,123456,654321);
    CHECK_EQ(data.size(),size_t(41));
    for(size_t i=0;i<8;++i) CHECK_EQ(data[i],word(ix["discriminator"].at(i)));
    CHECK_EQ(rd_u64(data.data()+8),uint64_t(123456)); CHECK_EQ(rd_u64(data.data()+16),uint64_t(654321));
    CHECK_EQ(rd_u128(data.data()+24),u128(0)); CHECK_EQ(data[40],uint8_t(1));
    for(bool zfo : {true,false}) {
      auto accounts=tx::dexix::swap_accounts(p,zfo,u);
      const std::vector<Pubkey> expected{u.authority,p.clmm.amm_config,p.address,zfo?u.token_a:u.token_b,zfo?u.token_b:u.token_a,
        zfo?p.vault_a:p.vault_b,zfo?p.vault_b:p.vault_a,p.clmm.observation_key,prog::TOKEN,prog::TOKEN_2022,prog::MEMO,
        zfo?p.mint_a:p.mint_b,zfo?p.mint_b:p.mint_a};
      CHECK_EQ(simdjson::dom::array(ix["accounts"]).size(),expected.size());
      for(size_t i=0;i<expected.size();++i) {
        CHECK(accounts[i].key == expected[i]);
        bool writable=false, signer=false;
        (void)ix["accounts"].at(i)["writable"].get(writable);
        (void)ix["accounts"].at(i)["signer"].get(signer);
        CHECK_EQ(accounts[i].writable,writable); CHECK_EQ(accounts[i].signer,signer);
      }
    }
    return;
  }
  CHECK(false); // IDL must contain swap_v2
}

TEST(clmm_quotes_cross_bitmap_boundaries_in_both_directions) {
  // Built-in <-> extension, and the next positive/negative 512-bit extension block.
  for (auto [current, zfo] : {std::pair{511,false}, {-512,true}, {-513,false}, {512,true},
                              {1023,false}, {-1024,true}, {-1025,false}, {1024,true}}) {
    auto p=model();
    const int direction=zfo?-1:1;
    p.clmm.tick_current=current*60+(zfo?0:59);
    p.clmm.sqrt_price_x64=*dex::clmm::sqrt_price_at_tick(p.clmm.tick_current);
    auto e=std::make_shared<state::ClmmBitmapExtension>(); e->pool=p.address;
    for(int n=0;n<3;++n) {
      int index=current+n*direction;
      set_bit(p.clmm,*e,index);
      add_tick(p,index*60,index*60+(zfo?59:0));
    }
    p.clmm_bitmap=e;
    auto target=*dex::clmm::sqrt_price_at_tick((current+direction)*60+(zfo?59:0));
    auto step=dex::clmm::compute_swap_exact_in(p.clmm.sqrt_price_x64,target,p.clmm.liquidity,
        100'000'000'000'000ULL,p.clmm_cfg.trade_fee_rate,zfo,true);
    CHECK(step); if(!step) continue;
    const auto amount=step->amount_in+step->fee_amount+10000;
    auto q=dex::ClmmQuoter{}.quote_exact_in(p,zfo,amount);
    CHECK(q); if(!q) continue;
    CHECK_EQ(q->amount_in,amount); CHECK(!q->cache_gap); CHECK(!q->crosses_boundary); CHECK(q->amount_out>0);
    auto accounts=tx::dexix::swap_accounts(p,zfo,{});
    CHECK_EQ(accounts.size(),size_t(17));
    for(size_t n=0;n<3;++n) CHECK(accounts[14+n].key==p.ticks[n].key);
  }
}
