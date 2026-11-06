#include "test.hpp"
#include "fixture.hpp"
#include "dex/dlmm.hpp"
#include "app/pools_file.hpp"
#include "app/runtime.hpp"
#include "core/crypto.hpp"
#include "tx/dex_ix.hpp"
#include <cstring>
using namespace arb;
using namespace arbtest;
namespace {
u128 integer(simdjson::dom::element e, unsigned base = 10) {
  std::string_view s;
  if (e.get(s) != simdjson::SUCCESS) return uint64_t(e);
  u128 n = 0;
  for (char c : s) n = n * base + (c <= '9' ? c - '0' : c - 'a' + 10);
  return n;
}
Pubkey key(simdjson::dom::element e) { return parse_pubkey(std::string_view(e)); }
struct Json { simdjson::dom::parser parser; simdjson::padded_string body{read_file(fixture_path("dlmm_sdk.json"))}; simdjson::dom::element root = parser.parse(body); };
PoolState pool(int32_t id = -1) {
  PoolState p; p.kind = DexKind::Dlmm; p.address = mint::WSOL; p.complete = p.dlmm_bitmap_known = true;
  p.dlmm.active_id = id; p.dlmm.bin_step = 25; p.dlmm.function_type = 2;
  p.pool_slot = p.vault_a_slot = p.vault_b_slot = p.dlmm_clock_slot = 100;
  p.dlmm_clock = {100,1000}; p.dlmm.last_update_timestamp = 1000;
  p.dlmm.filter_period = 10; p.dlmm.decay_period = 120;
  p.dlmm.max_volatility_accumulator = 1000000;
  return p;
}
std::shared_ptr<state::DlmmBinArray> add_array(PoolState& p, int32_t index) {
  auto a = std::make_shared<state::DlmmBinArray>(); a->index = index; a->pair = p.address;
  p.dlmm_arrays.push_back({index,state::dlmm_bin_array_pda(p.address,index),a});
  if (index >= -512 && index < 512) { unsigned b = unsigned(index+512); p.dlmm.bin_array_bitmap[b/64] |= uint64_t(1) << (b%64); }
  return a;
}
}
TEST(dlmm_independent_official_idl_decoders) {
  Json f;
  CHECK(std::string_view(f.root["commit"]) == "1532e540db41ac405dbf4c5bb2ddfceaa1f98ff1");
  size_t count = 0;
  for (auto v : simdjson::dom::array(f.root["accounts"])) {
    std::vector<uint8_t> bytes; CHECK(b64_decode(std::string_view(v["data"]), bytes));
    auto e = v["decoded"]; std::string_view type = v["type"];
    const auto owner = key(v["owner"]);
    if (type == "lbPair") {
      auto p = state::decode_dlmm_pair(bytes.data(),bytes.size(),owner); CHECK(p.has_value()); if (!p) continue;
      auto s = e["parameters"], x = e["vParameters"];
#define N(field, obj, name) CHECK_EQ(p->field, integer(obj[name]))
      N(base_factor,s,"baseFactor"); N(filter_period,s,"filterPeriod"); N(decay_period,s,"decayPeriod");
      N(reduction_factor,s,"reductionFactor"); N(variable_fee_control,s,"variableFeeControl");
      N(max_volatility_accumulator,s,"maxVolatilityAccumulator"); N(protocol_share,s,"protocolShare");
      N(base_fee_power_factor,s,"baseFeePowerFactor"); N(function_type,s,"functionType"); N(collect_fee_mode,s,"collectFeeMode");
      CHECK_EQ(p->min_bin_id,int64_t(s["minBinId"])); CHECK_EQ(p->max_bin_id,int64_t(s["maxBinId"]));
      N(volatility_accumulator,x,"volatilityAccumulator"); N(volatility_reference,x,"volatilityReference");
      CHECK_EQ(p->index_reference,int64_t(x["indexReference"])); CHECK_EQ(p->last_update_timestamp,int64_t(integer(x["lastUpdateTimestamp"],16)));
      CHECK_EQ(p->active_id,int64_t(e["activeId"])); N(bin_step,e,"binStep"); N(status,e,"status"); N(pair_type,e,"pairType");
      N(activation_type,e,"activationType"); CHECK_EQ(p->activation_point,integer(e["activationPoint"],16));
      N(token_x_program_flag,e,"tokenMintXProgramFlag"); N(token_y_program_flag,e,"tokenMintYProgramFlag");
#undef N
      CHECK(p->token_x_mint==key(e["tokenXMint"]) && p->token_y_mint==key(e["tokenYMint"]));
      CHECK(p->reserve_x==key(e["reserveX"]) && p->reserve_y==key(e["reserveY"]) && p->oracle==key(e["oracle"]));
      size_t i=0; for(auto n:simdjson::dom::array(e["binArrayBitmap"])) CHECK_EQ(p->bin_array_bitmap[i++],integer(n,16));
      bool rewards=false; for(auto r:simdjson::dom::array(e["rewardInfos"])) rewards |= !is_zero(key(r["mint"]));
      CHECK(p->has_rewards==rewards);
      CHECK(!state::decode_dlmm_pair(bytes.data(),bytes.size()-1,owner));
      CHECK(!state::decode_dlmm_pair(bytes.data(),bytes.size(),prog::TOKEN));
      bytes[0]^=1; CHECK(!state::decode_dlmm_pair(bytes.data(),bytes.size(),owner));
    } else if (type == "binArray") {
      auto p=state::decode_dlmm_bin_array(bytes.data(),bytes.size(),owner); CHECK(p.has_value()); if(!p) continue;
      std::string_view hex=e["index"]; bool negative=hex.front()=='-';
      u128 n=0; for(char c:hex.substr(negative?1:0)) n=n*16+(c<='9'?c-'0':c-'a'+10);
      CHECK_EQ(p->index,negative?-int64_t(n):int64_t(n)); CHECK(p->pair==key(e["lbPair"]));
      size_t i=0; for(auto b:simdjson::dom::array(e["bins"])) {
        auto& got=p->bins[i++]; CHECK_EQ(got.amount_x,integer(b["amountX"],16)); CHECK_EQ(got.amount_y,integer(b["amountY"],16));
        CHECK_EQ(got.price,integer(b["price"],16)); CHECK_EQ(got.open_order_amount,integer(b["openOrderAmount"],16));
        CHECK_EQ(got.processed_order_remaining,integer(b["processedOrderRemainingAmount"],16));
        CHECK_EQ(got.limit_order_ask_side,bool(uint64_t(b["limitOrderAskSide"])));
      }
      CHECK(!state::decode_dlmm_bin_array(bytes.data(),bytes.size()-1,owner));
      CHECK(!state::decode_dlmm_bin_array(bytes.data(),bytes.size(),prog::TOKEN));
      bytes[0]^=1; CHECK(!state::decode_dlmm_bin_array(bytes.data(),bytes.size(),owner));
    } else {
      auto b=state::decode_dlmm_bitmap(bytes.data(),bytes.size(),owner); CHECK(b.has_value()); if(!b) continue;
      CHECK(b->pair==key(e["lbPair"]));
      for(auto [name,ptr]:{std::pair{"positiveBinArrayBitmap",b->positive},std::pair{"negativeBinArrayBitmap",b->negative}}) {
        size_t i=0; for(auto row:simdjson::dom::array(e[name])) for(auto n:simdjson::dom::array(row)) CHECK_EQ(ptr[i++],integer(n,16));
      }
      CHECK(!state::decode_dlmm_bitmap(bytes.data(),bytes.size()-1,owner));
      CHECK(!state::decode_dlmm_bitmap(bytes.data(),bytes.size(),prog::TOKEN));
      bytes[0]^=1; CHECK(!state::decode_dlmm_bitmap(bytes.data(),bytes.size(),owner));
    }
    ++count;
  }
  CHECK_EQ(count,size_t(7));
}
TEST(dlmm_sdk_integer_prices_and_variable_fee_quotes) {
  Json f;
  for(auto v:simdjson::dom::array(f.root["prices"])) {
    auto px=dex::dlmm::price(int32_t(int64_t(v["id"])),uint16_t(uint64_t(v["step"]))); CHECK(px.has_value());
    if(px) CHECK_EQ(*px,integer(v["price"]));
  }
  size_t count=0;
  for(auto v:simdjson::dom::array(f.root["cases"])) {
    int32_t active=int32_t(int64_t(v["active"])); bool down=bool(v["down"]);
    auto p=pool(active); p.dlmm.base_factor=10000; p.dlmm.reduction_factor=1000;
    p.dlmm.variable_fee_control=40000; p.dlmm.protocol_share=1500;
    p.dlmm.volatility_accumulator=200000; p.dlmm.volatility_reference=30000; p.dlmm.index_reference=active+2;
    p.dlmm.collect_fee_mode=uint8_t(uint64_t(v["mode"])); p.dlmm_clock.unix_timestamp+=int64_t(v["elapsed"]);
    auto a=add_array(p,dex::dlmm::array_index(active)); auto& b=a->bins[size_t(active-a->index*70)];
    b.amount_x=11000; b.amount_y=17000; b.open_order_amount=7000; b.processed_order_remaining=3000;
    b.limit_order_ask_side=!down; b.price=integer(v["price"]);
    auto ctx=dex::dlmm::fee_context(p); CHECK(ctx.has_value());
    if(ctx) {auto rate=dex::dlmm::fee_rate(*ctx,active); CHECK(rate.has_value()); if(rate) CHECK_EQ(*rate,integer(v["rate"]));}
    // For drained-bin cases quote precisely the SDK's consumed input (no invented subsequent liquidity).
    auto q=dex::DlmmQuoter{}.quote_exact_in(p,down,uint64_t(integer(v["amount_in"])));
    if (integer(v["amount_out"]) == 0) { CHECK(!q); ++count; continue; }
    CHECK(q.has_value()); if(q) {CHECK(!q->crosses_boundary);CHECK_EQ(q->amount_in,integer(v["amount_in"])); CHECK_EQ(q->amount_out,integer(v["amount_out"])); CHECK_EQ(q->fee_paid,integer(v["fee"]));}
    ++count;
  }
  CHECK_EQ(count,size_t(320));
}
TEST(dlmm_negative_bitmap_boundaries_and_cache_fail_closed) {
  CHECK_EQ(dex::dlmm::array_index(-1),-1); CHECK_EQ(dex::dlmm::array_index(-70),-1); CHECK_EQ(dex::dlmm::array_index(-71),-2);
  auto p=pool(-35910); auto e=std::make_shared<state::DlmmBitmapExtension>(); e->pair=p.address;
  e->negative[0]=1; e->negative[95]=uint64_t(1)<<63; e->positive[0]=1; e->positive[95]=uint64_t(1)<<63; p.dlmm_bitmap=e;
  for(int i:{-6656,-513,512,6655}) CHECK(dex::dlmm::initialized(p.dlmm,e.get(),i));
  for(int i:{-6657,-514,-512,511,513,6656}) CHECK(!dex::dlmm::initialized(p.dlmm,e.get(),i));
  int32_t n; CHECK(dex::dlmm::next_array(p,-512,true,n));CHECK_EQ(n,-513);
  CHECK(dex::dlmm::next_array(p,511,false,n));CHECK_EQ(n,512);
  auto q=dex::DlmmQuoter{}.quote_exact_in(p,true,1000);CHECK(q && q->crosses_boundary && q->cache_gap && q->amount_in==0);
  auto a=add_array(p,-513); a->bins[0].amount_y=10000; a->bins[0].price=dex::dlmm::ONE;
  q=dex::DlmmQuoter{}.quote_exact_in(p,true,1000); CHECK(q && !q->crosses_boundary && q->amount_out==1000);
  p.dlmm_clock_slot=99;CHECK(!dex::DlmmQuoter{}.quote_exact_in(p,true,1000));
  p.dlmm_clock_slot=100;p.dlmm_clock.unix_timestamp=999;CHECK(!dex::DlmmQuoter{}.quote_exact_in(p,true,1000));
  p.dlmm_clock.unix_timestamp=1000;p.dlmm.token_x_program_flag=1;CHECK(!dex::DlmmQuoter{}.supports(p));
}
TEST(dlmm_negative_bin_crossing_and_three_array_limit) {
  auto p=pool(0); auto a=add_array(p,0); auto b=add_array(p,-1);
  a->bins[0].amount_y=100; a->bins[0].price=dex::dlmm::ONE;
  b->bins[69].amount_y=1000; b->bins[69].price=dex::dlmm::ONE/2;
  auto q=dex::DlmmQuoter{}.quote_exact_in(p,true,300); CHECK(q && q->amount_out==200 && q->amount_in==300);
  CHECK_EQ(dex::input_depth(p,true),uint64_t(100)); CHECK_EQ(dex::spot_rate_x64(p,true),dex::dlmm::ONE);
  p.dlmm.active_id=-1;CHECK_EQ(dex::dlmm::mid(p,true),*dex::dlmm::price(-1,25));
  p=pool(0);
  for(int i=0;i>-4;--i) {auto arr=add_array(p,i); arr->bins[i==0?0:69].amount_y=100;arr->bins[i==0?0:69].price=dex::dlmm::ONE;}
  q=dex::DlmmQuoter{}.quote_exact_in(p,true,400); CHECK(q && q->crosses_boundary && !q->cache_gap && q->amount_in==300 && q->amount_out==300);
}
TEST(dlmm_swap2_accounts_data_and_disabled_gate) {
  auto p=pool(-1); p.mint_a=mint::WSOL;p.mint_b=mint::USDC;p.vault_a=mint::USDT;p.vault_b=prog::TOKEN;p.dlmm.oracle=prog::SYSTEM;
  auto a=add_array(p,-1);a->bins[69].amount_y=1000;
  tx::dexix::UserAccounts u{prog::ATA,mint::WSOL,mint::USDC};
  for(bool down:{true,false}) {
    auto m=tx::dexix::swap_accounts(p,down,u);CHECK_EQ(m.size(),size_t(17));
    CHECK(m[0].key==p.address && m[0].writable);CHECK(m[1].key==prog::METEORA_DLMM && !m[1].writable);
    CHECK(m[2].key==p.vault_a && m[3].key==p.vault_b);CHECK(m[4].key==(down?u.token_a:u.token_b));
    CHECK(m[5].key==(down?u.token_b:u.token_a));CHECK(m[6].key==p.mint_a && m[7].key==p.mint_b);
    CHECK(m[8].key==p.dlmm.oracle && m[8].writable);CHECK(m[9].key==prog::METEORA_DLMM);
    CHECK(m[10].key==u.authority && m[10].signer);CHECK(m[11].key==prog::TOKEN && m[12].key==prog::TOKEN && m[13].key==prog::MEMO);
    CHECK(m[14].key==pda({std::string_view("__event_authority")},prog::METEORA_DLMM));CHECK(m[15].key==prog::METEORA_DLMM);
    CHECK(m[16].key==state::dlmm_bin_array_pda(p.address,-1) && m[16].writable);
    auto d=tx::dexix::swap_data(DexKind::Dlmm,down,12345,678);
    CHECK_EQ(d.size(),size_t(28));CHECK(std::memcmp(d.data(),tx::dexix::DLMM_SWAP2_DISC,8)==0);
    CHECK_EQ(rd_u64(d.data()+8),uint64_t(12345));CHECK_EQ(rd_u64(d.data()+16),uint64_t(678));CHECK_EQ(rd_u32(d.data()+24),uint32_t(0));
  }
  CHECK(!Config{}.dex.dlmm_enabled && !Config{}.discovery.track_dlmm);
  Config cfg;cfg.dex.dlmm_enabled=true;
  bool threw=false;try { app::Runtime runtime(cfg, {}); } catch(const std::invalid_argument&) {threw=true;}
  CHECK(!threw);
}
TEST(dlmm_bitmap_and_swap2_match_pinned_sdk) {
  Json f;
  auto p=pool(); auto ext=std::make_shared<state::DlmmBitmapExtension>();ext->pair=p.address;
  ext->negative[0]=ext->positive[0]=1; ext->negative[95]=ext->positive[95]=uint64_t(1)<<63;p.dlmm_bitmap=ext;
  p.dlmm.bin_array_bitmap[0]=1;p.dlmm.bin_array_bitmap[15]=uint64_t(1)<<63;
  for(auto c:simdjson::dom::array(f.root["bitmapCases"])) {
    int32_t result=0;bool found=dex::dlmm::next_array(p,int32_t(int64_t(c["from"])),bool(c["down"]),result);
    if(c["next"].is_null()) CHECK(!found);else {CHECK(found);CHECK_EQ(result,int64_t(c["next"]));}
  }
  for(auto v:simdjson::dom::array(f.root["accounts"])) if(std::string_view(v["type"])=="lbPair") {
    std::vector<uint8_t> data;b64_decode(std::string_view(v["data"]),data);
    auto pair=state::decode_dlmm_pair(data.data(),data.size(),prog::METEORA_DLMM);CHECK(pair.has_value());if(!pair) continue;
    p=pool(pair->active_id);p.address=key(v["key"]);p.dlmm=*pair;
    p.mint_a=pair->token_x_mint;p.mint_b=pair->token_y_mint;p.vault_a=pair->reserve_x;p.vault_b=pair->reserve_y;
    add_array(p,0);
    for(auto c:simdjson::dom::array(f.root["instructionCases"])) {
      bool down=bool(c["down"]);
      auto accounts=tx::dexix::swap_accounts(p,down,{prog::SYSTEM,p.mint_a,p.mint_b});
      auto data=tx::dexix::swap_data(DexKind::Dlmm,down,12345,678);
      std::vector<uint8_t> expected;b64_decode(std::string_view(c["data"]),expected);CHECK(data==expected);
      size_t i=0;for(auto a:simdjson::dom::array(c["accounts"])) {
        CHECK(i<accounts.size());if(i>=accounts.size())break;
        CHECK(accounts[i].key==key(a["key"]));CHECK(accounts[i].signer==bool(a["signer"]));CHECK(accounts[i].writable==bool(a["writable"]));++i;
      }
      CHECK_EQ(accounts.size(),i);
    }
  }
}
TEST(dlmm_store_roundtrip_dependencies_and_poisoned_update) {
  Json f;
  for(auto v:simdjson::dom::array(f.root["accounts"])) if(std::string_view(v["type"])=="lbPair") {
    state::RawAccount raw;raw.key=key(v["key"]);raw.owner=prog::METEORA_DLMM;b64_decode(std::string_view(v["data"]),raw.data);
    auto d=state::pool_def_from_account(raw,{});CHECK(d.has_value());if(!d)continue;
    CHECK(d->kind==DexKind::Dlmm && d->bitmap_extension==state::dlmm_bitmap_pda(d->address));
    app::PoolsFile pf;pf.pools.push_back(*d);pf.tvl_sol.push_back(100);
    app::PoolsFile parsed;std::string err;CHECK(app::from_json(app::to_json(pf),parsed,&err));CHECK_EQ(parsed.pools.size(),size_t(1));
    CHECK(parsed.pools[0].bin_arrays==d->bin_arrays && parsed.pools[0].bitmap_extension==d->bitmap_extension);
    StateStore store;auto id=store.add_pool(*d);
    auto apply=[&](Pubkey k,Pubkey owner,const std::vector<uint8_t>& b,uint64_t slot=100,bool missing=false,bool resync=false) {
      AccountUpdate u;u.key=k;u.owner=owner;u.data=b.data();u.len=b.size();u.slot=slot;u.missing=missing;u.from_resync=resync;return store.apply(u);
    };
    auto keys=store.subscribed_keys();CHECK(std::find(keys.begin(),keys.end(),state::DLMM_CLOCK)!=keys.end());
    CHECK(apply(raw.key,raw.owner,raw.data));
    for(auto mint:{d->mint_a,d->mint_b}) {std::vector<uint8_t> b(82);b[44]=9;b[45]=1;CHECK(apply(mint,prog::TOKEN,b));}
    for(auto vault:{d->vault_a,d->vault_b}) {std::vector<uint8_t> b(165);b[108]=1;uint64_t amount=100000;std::memcpy(b.data()+64,&amount,8);CHECK(apply(vault,prog::TOKEN,b));}
    std::vector<uint8_t> clock(40);uint64_t slot=100;int64_t now=2000000000;std::memcpy(clock.data(),&slot,8);std::memcpy(clock.data()+32,&now,8);
    CHECK(apply(state::DLMM_CLOCK,PK("Sysvar1111111111111111111111111111111111111"),clock));
    CHECK(!store.pool(id)->complete);CHECK(apply(d->bitmap_extension,{}, {},100,true));CHECK(store.pool(id)->complete);
    CHECK(store.pool(id)->quotable(2));
    for(auto a:simdjson::dom::array(f.root["accounts"])) if(std::string_view(a["type"])=="binArray" && std::string_view(a["key"])!="synthetic-negative") {
      std::vector<uint8_t> b;b64_decode(std::string_view(a["data"]),b);CHECK(apply(key(a["key"]),prog::METEORA_DLMM,b));
    }
    CHECK(store.pool(id)->dlmm_arrays.size()==d->bin_arrays.size());
    auto wrong=raw.data;wrong[0]^=1;CHECK(!apply(raw.key,raw.owner,wrong,101));CHECK(!store.pool(id)->quotable(2));
    CHECK(apply(raw.key,raw.owner,raw.data,101,false,true));CHECK(!store.pool(id)->quotable(2));
    CHECK(apply(raw.key,raw.owner,raw.data,101));CHECK(!store.pool(id)->quotable(2)); // Clock still at 100
    slot=101;std::memcpy(clock.data(),&slot,8);CHECK(apply(state::DLMM_CLOCK,PK("Sysvar1111111111111111111111111111111111111"),clock,101));CHECK(store.pool(id)->quotable(2));
    store.mark_suspect(d->bin_arrays[0].second);CHECK(!store.pool(id)->quotable(2));
    CHECK(apply(d->bin_arrays[0].second,{}, {},101,true));CHECK(store.pool(id)->quotable(2));
    CHECK(apply(d->mint_a,{}, {},101,true));CHECK(!store.pool(id)->complete);
  }
}

TEST(dlmm_volatility_reduction_checks_scaled_value) {
  auto p = pool();
  p.dlmm.filter_period = 1; p.dlmm.decay_period = 100;
  p.dlmm.last_update_timestamp = p.dlmm_clock.unix_timestamp - 2;
  p.dlmm.volatility_accumulator = 2'000'000;
  p.dlmm.reduction_factor = 5000;
  auto ctx = dex::dlmm::fee_context(p);
  CHECK(ctx.has_value());
  if (ctx) CHECK_EQ(ctx->volatility_reference, uint32_t(1'000'000));
}

TEST(dlmm_current_clock_keeps_unchanged_pool_quotable) {
  auto p=pool(); add_array(p,-1);
  p.dlmm_clock_slot=p.dlmm_clock.slot=10000;
  p.dlmm_clock.unix_timestamp=2000;
  CHECK(p.quotable(2));
  CHECK(dex::dlmm::fee_context(p).has_value());
  p.dlmm_clock_slot=p.dlmm_clock.slot=99;
  CHECK(!p.quotable(2));
  CHECK(!dex::dlmm::fee_context(p));
}
