// Phase 2: route graph, prefilter, sizer, cost model, tip policy, dispatcher and the engine loop.
#include <cstring>

#include "app/bundle_sim.hpp"
#include "core/sys.hpp"
#include "core/usage.hpp"
#include "dex/clmm_math.hpp"
#include "core/crypto.hpp"
#include "tx/dex_ix.hpp"
#include "core/programs.hpp"
#include "obs/replay_report.hpp"
#include "engine/engine.hpp"
#include "fixture.hpp"
#include "flash/kamino.hpp"
#include "test.hpp"

using namespace arb;
using namespace arb::engine;
using namespace arbtest;

namespace {

Pubkey key_n(uint8_t n) {
  Pubkey k{};
  k[0] = n;
  k[31] = 0xAB;
  return k;
}

PoolDef def(uint8_t id, const Pubkey& a, const Pubkey& b, DexKind kind = DexKind::RaydiumV4) {
  PoolDef d;
  d.kind = kind;
  d.address = key_n(id);
  d.mint_a = a;
  d.mint_b = b;
  d.vault_a = key_n(uint8_t(100 + id));
  d.vault_b = key_n(uint8_t(150 + id));
  return d;
}

/** A Raydium v4 snapshot with the given coin/pc reserves and a 25 bps swap fee. */
std::shared_ptr<const PoolState> ray(uint32_t id, const Pubkey& coin, const Pubkey& pc, uint64_t coin_amt, uint64_t pc_amt) {
  auto p = std::make_shared<PoolState>();
  p->kind = DexKind::RaydiumV4;
  p->id = id;
  p->mint_a = coin;
  p->mint_b = pc;
  p->vault_a_amount = coin_amt;
  p->vault_b_amount = pc_amt;
  p->ray.status = 6;
  p->ray.coin_mint = coin;
  p->ray.pc_mint = pc;
  p->ray.swap_fee_numerator = 25;
  p->ray.swap_fee_denominator = 10000;
  p->complete = true;
  return p;
}

Quoters both_quoters() {
  static dex::RaydiumV4Quoter rq;
  static dex::WhirlpoolQuoter wq;
  Quoters q;
  q.by_kind[uint8_t(DexKind::RaydiumV4)] = &rq;
  q.by_kind[uint8_t(DexKind::Whirlpool)] = &wq;
  return q;
}

Cycle two_hop(uint32_t p1, bool d1, uint32_t p2, bool d2, const Pubkey& base, const Pubkey& x) {
  Cycle c;
  c.n = 2;
  c.base = base;
  c.legs[0] = Leg{p1, d1, base, x};
  c.legs[1] = Leg{p2, d2, x, base};
  DexKind k[2] = {DexKind::RaydiumV4, DexKind::RaydiumV4};
  c.route_class = route_class_of(2, k);
  return c;
}

}  // namespace

TEST(isqrt_known_values) {
  CHECK_EQ(isqrt_u128(0), u128(0));
  CHECK_EQ(isqrt_u128(1), u128(1));
  CHECK_EQ(isqrt_u128(15), u128(3));
  CHECK_EQ(isqrt_u128(16), u128(4));
  CHECK_EQ(isqrt_u128(u128(UINT64_MAX) * UINT64_MAX), u128(UINT64_MAX));
  u128 big = (u128(1) << 127) + 12345;
  u128 r = isqrt_u128(big);
  CHECK(r * r <= big && (r + 1) * (r + 1) > big);
}

TEST(route_graph_two_and_three_hop_cycles) {
  Pubkey B = mint::WSOL, X = key_n(1), Y = key_n(2);
  std::vector<PoolDef> pools = {def(10, B, X), def(11, X, B), def(12, X, Y), def(13, Y, B)};
  std::vector<double> tvl = {100, 90, 80, 70};
  GraphConfig g;
  g.base_mints = {B};
  CycleIndex idx;
  idx.build(pools, tvl, g);
  size_t two = 0, three = 0;
  for (auto& c : idx.all()) {
    (c.n == 2 ? two : three)++;
    CHECK(c.legs[0].mint_in == B && c.legs[c.n - 1].mint_out == B);
    for (int i = 0; i + 1 < c.n; ++i) CHECK(c.legs[i].mint_out == c.legs[i + 1].mint_in);
    // Leg direction matches the pool's mint order.
    for (int i = 0; i < c.n; ++i) {
      const PoolDef& p = pools[c.legs[i].pool];
      CHECK((c.legs[i].a_to_b ? p.mint_a : p.mint_b) == c.legs[i].mint_in);
    }
  }
  CHECK_EQ(two, size_t(2));    // P0 then P1, P1 then P0
  CHECK_EQ(three, size_t(4));  // B-X-Y-B via P0 or P1, and the reverse
  CHECK_EQ(idx.cycles_of(2).size(), size_t(4));
  CHECK_EQ(idx.cycles_of(0).size(), size_t(4));  // two 2-hop, two 3-hop
  // Deepest first: the first cycle kept is the 2-hop through the two deepest pools.
  CHECK_EQ(int(idx.cycle(0).n), 2);
  g.max_cycles_per_pool = 1;
  idx.build(pools, tvl, g);
  for (uint32_t p = 0; p < pools.size(); ++p) CHECK(idx.cycles_of(p).size() <= 1);
  g.max_cycles_per_pool = 64;
  g.max_hops = 2;
  idx.build(pools, tvl, g);
  CHECK_EQ(idx.size(), size_t(2));
}

TEST(prefilter_spot_edge) {
  Pubkey B = mint::WSOL, X = key_n(1);
  // Same price in both pools: the cycle loses two fees, so the product is below 1.
  Snapshots s{ray(0, B, X, 1000'000'000'000ULL, 100'000'000'000ULL), ray(1, B, X, 1000'000'000'000ULL, 100'000'000'000ULL)};
  Cycle c = two_hop(0, true, 1, false, B, X);
  CHECK(spot_product_x64(c, s) < edge_threshold_x64(0));
  // 2% cheaper X in pool 0: about +1.5% after two 25 bps fees.
  s[0] = ray(0, B, X, 1000'000'000'000ULL, 102'000'000'000ULL);
  u128 p = spot_product_x64(c, s);
  CHECK(p > edge_threshold_x64(10'000));
  CHECK(p < edge_threshold_x64(20'000));
}

TEST(sizer_matches_closed_form_and_brute_force) {
  Pubkey B = mint::WSOL, X = key_n(1);
  Snapshots s{ray(0, B, X, 1000'000'000'000ULL, 102'000'000'000ULL), ray(1, B, X, 1000'000'000'000ULL, 100'000'000'000ULL)};
  Cycle c = two_hop(0, true, 1, false, B, X);
  Quoters q = both_quoters();
  FlashFeeFn fee;  // own capital: no flash fee
  // Brute force over a fine grid of the exact profit function.
  int64_t best_p = INT64_MIN;
  uint64_t best_x = 0;
  for (uint64_t x = 100'000'000; x <= 40'000'000'000ULL; x += 10'000'000) {
    ChainQuote ch = quote_chain(c, s, q, x);
    CHECK(ch.ok);
    int64_t p = int64_t(ch.final_out) - int64_t(x);
    if (p > best_p) { best_p = p; best_x = x; }
  }
  uint64_t cf = closed_form_2hop_cp(c, s);
  CHECK(cf > 0);
  double rel = double(cf > best_x ? cf - best_x : best_x - cf) / double(best_x);
  std::printf("    closed form %llu vs brute %llu (rel %.4f%%), brute profit %lld\n", (unsigned long long)cf,
              (unsigned long long)best_x, rel * 100, (long long)best_p);
  CHECK(rel < 0.01);
  SizerConfig sc;
  sc.min_trade = 1'000'000;
  sc.x_max = 200'000'000'000ULL;
  SizeResult r = size_cycle(c, s, q, fee, sc);
  CHECK(r.ok);
  CHECK(r.evals <= 24);
  std::printf("    sizer x=%llu profit=%lld evals=%d\n", (unsigned long long)r.x, (long long)r.profit, r.evals);
  CHECK(r.profit >= best_p - best_p / 100000);  // within 0.001% of the grid optimum (grid step is coarse)
  // The seedless search (3-hop and CLMM routes) also gets close.
  SizerConfig sc2 = sc;
  sc2.use_closed_form = false;
  sc2.x_max = 40'000'000'000ULL;
  SizeResult r2 = size_cycle(c, s, q, fee, sc2);
  std::printf("    seedless x=%llu profit=%lld evals=%d\n", (unsigned long long)r2.x, (long long)r2.profit, r2.evals);
  CHECK(r2.ok && r2.profit > best_p * 99 / 100);
  // No edge: no size.
  Snapshots flat{ray(0, B, X, 1000'000'000'000ULL, 100'000'000'000ULL), ray(1, B, X, 1000'000'000'000ULL, 100'000'000'000ULL)};
  CHECK_EQ(closed_form_2hop_cp(c, flat), uint64_t(0));
  CHECK(!size_cycle(c, flat, q, fee, sc).ok);
  // Depth bound: 20% of the shallowest input-side depth, in base units.
  uint64_t db = depth_bound(c, s, 200'000);
  CHECK(db <= 200'000'000'000ULL && db > 190'000'000'000ULL);
}

TEST(flash_fee_rounding_and_disabled) {
  constexpr uint64_t ONE = uint64_t(1) << 60;
  // 0.09% (9 bps): fee_sf = 0.0009 * 2^60.
  uint64_t sf = ONE / 10000 * 9;
  auto f = flash::flash_loan_fee(1'000'000'000, sf);
  CHECK(f.has_value());
  CHECK(*f == 900'000 || *f == 899'999);
  CHECK_EQ(*flash::flash_loan_fee(1'000'000'000, 0), uint64_t(0));
  CHECK(!flash::flash_loan_fee(1'000'000'000, UINT64_MAX).has_value());
  CHECK_EQ(*flash::flash_loan_fee(10, sf), uint64_t(1));  // minimum fee of one unit
  FlashFeeFn fn{sf, true};
  uint64_t out = 0;
  CHECK(fn(2'000'000'000, out) && out >= 1'799'999);
}

TEST(cost_model_tip_guard_and_ev) {
  CostConfig cc;
  TipPolicy tips;
  BasePrice sol;  // wSOL: 1 lamport per unit
  // Gross 1,000,000 lamports: tip = 50% = 500,000; fee 5,000; net 495,000; EV = 10% of net.
  Pricing pr = price(5'000'000'000ULL, 1'000'000, 0, 100'000, 0x0201, sol, tips, cc);
  CHECK(pr.ok);
  CHECK_EQ(pr.tip_lamports, uint64_t(500'000));
  CHECK_EQ(pr.fee_lamports, uint64_t(5000));
  CHECK_EQ(pr.net_lamports, int64_t(495'000));
  CHECK_EQ(pr.ev_lamports, int64_t(49'500));
  CHECK_EQ(pr.min_out, uint64_t(5'000'000'000ULL + 500'000 + 10'000));
  CHECK_EQ(pr.cu_limit, uint32_t((100'000 + 60'000) * 115 / 100));
  // Small gross: the tip is cut so net stays at min_net; EV then falls under min_ev.
  Pricing small = price(1'000'000'000ULL, 20'000, 0, 100'000, 0x0201, sol, tips, cc);
  CHECK(!small.ok);
  CHECK(small.reject == Reason::below_min_ev);
  CHECK_EQ(small.net_lamports, int64_t(10'000));
  CHECK(price(1, 14'000, 0, 100'000, 0x0201, sol, tips, cc).reject == Reason::below_min_net);
  // Tip floor from Jito's p25 raises the minimum tip.
  tips.set_tip_floor_p25(800'000);
  Pricing floored = price(5'000'000'000ULL, 1'200'000, 0, 100'000, 0x0201, sol, tips, cc);
  CHECK(floored.ok && floored.tip_lamports == 800'000);
  // CU budget.
  CHECK(price(1, 1'000'000, 0, 1'100'000, 0x0301, sol, tips, cc).reject == Reason::cu_over_budget);
  // USDC base: 1 USDC unit (1e-6 USDC) worth 0.006 lamports at 6000 lamports... use 150 USDC/SOL:
  // 1 unit = 1e9 / 150e6 lamports = 6.666.. lamports, Q64.64.
  BasePrice usdc{(u128(1'000'000'000) << 64) / 150'000'000, true};
  TipPolicy t2;
  Pricing pu = price(1'000'000'000, 600'000, 900, 100'000, 0x0201, usdc, t2, cc);  // 0.6 USDC gross ~ 4.0M lamports
  CHECK(pu.ok);
  // Guard: x + flash fee + ceil(tip in USDC units) + ceil(min_net in USDC units) <= what the quote delivers.
  CHECK(pu.min_out <= 1'000'000'000ULL + 900 + 600'000);
  CHECK(pu.min_out >= 1'000'000'000ULL + 900 + usdc.from_lamports_ceil(pu.tip_lamports));
}

TEST(tip_policy_window_controller) {
  TipPolicy::Params p;
  TipPolicy t(p);
  uint16_t rc = 0x0201;
  CHECK_EQ(t.alpha_ppm(rc), uint32_t(500'000));
  for (int i = 0; i < 50; ++i) t.record(rc, false);
  CHECK_EQ(t.alpha_ppm(rc), uint32_t(550'000));
  for (int i = 0; i < 50; ++i) t.record(rc, true);
  CHECK_EQ(t.alpha_ppm(rc), uint32_t(500'000));
  for (int k = 0; k < 20; ++k)
    for (int i = 0; i < 50; ++i) t.record(rc, false);
  CHECK_EQ(t.alpha_ppm(rc), uint32_t(950'000));
  for (int k = 0; k < 30; ++k)
    for (int i = 0; i < 50; ++i) t.record(rc, true);
  CHECK_EQ(t.alpha_ppm(rc), uint32_t(200'000));
  // p_land from history, starting at the prior.
  CHECK_EQ(t.p_land_ppm(0x0301), uint32_t(100'000));
  CHECK(t.p_land_ppm(rc) > 500'000);
}

TEST(dispatcher_conflict_cap_lag_cooldown) {
  Pubkey B = mint::WSOL, X = key_n(1), Y = key_n(2);
  std::vector<PoolDef> pools = {def(10, B, X), def(11, X, B), def(12, B, Y), def(13, Y, B), def(14, B, X),
                                def(15, B, Y), def(16, Y, B)};
  GraphConfig g;
  g.base_mints = {B};
  g.max_hops = 2;
  CycleIndex idx;
  idx.build(pools, {}, g);
  auto find = [&](uint32_t p1, uint32_t p2) {
    for (auto& c : idx.all())
      if (c.legs[0].pool == p1 && c.legs[1].pool == p2) return c.id;
    return UINT32_MAX;
  };
  auto opp = [&](uint32_t cyc, int64_t ev, uint64_t slot) {
    Opportunity o;
    o.cycle = cyc;
    o.slot = slot;
    o.pr.ev_lamports = ev;
    return o;
  };
  DispatchConfig dc;
  dc.max_bundles_per_slot = 2;
  Dispatcher d(dc);
  d.push(opp(find(0, 1), 100, 50));  // best
  d.push(opp(find(1, 0), 90, 50));   // shares pools 0 and 1 with the best
  d.push(opp(find(2, 3), 80, 50));
  d.push(opp(find(0, 4), 70, 50));   // shares pool 0
  d.push(opp(find(4, 1), 60, 48));   // stale: 2 slots behind
  std::vector<uint32_t> sent;
  std::vector<std::pair<uint32_t, Reason>> skipped;
  auto send = [&](const Opportunity& o) { sent.push_back(o.cycle); };
  auto skip = [&](const Opportunity& o, Reason r) { skipped.emplace_back(o.cycle, r); };
  d.dispatch(50, idx, send, skip);
  CHECK_EQ(sent.size(), size_t(2));
  CHECK(sent[0] == find(0, 1) && sent[1] == find(2, 3));
  CHECK_EQ(skipped.size(), size_t(3));
  CHECK(skipped[0].second == Reason::pool_conflict && skipped[1].second == Reason::pool_conflict);
  CHECK(skipped[2].second == Reason::send_lag);
  CHECK(d.cooling(find(0, 1), 52) && !d.cooling(find(0, 1), 53));
  // Slot cap: a third non-conflicting candidate in the same slot is skipped.
  d.push(opp(find(5, 6), 50, 50));
  sent.clear();
  skipped.clear();
  d.dispatch(50, idx, send, skip);
  CHECK(sent.empty() && skipped.size() == 1 && skipped[0].second == Reason::slot_cap);
  // Next slot: the cap resets.
  d.push(opp(find(5, 6), 50, 51));
  d.dispatch(51, idx, send, skip);
  CHECK_EQ(sent.size(), size_t(1));
}

namespace {

/** Load every fixture pool into `store` at one slot, as a stream push would. */
std::vector<PoolDef> load_fixture_pools(StateStore& store, uint64_t slot) {
  std::string body = read_file(fixture_path("pools.json"));
  simdjson::dom::parser p;
  simdjson::padded_string ps(body);
  simdjson::dom::element root = p.parse(ps);
  state::DiscoveryConfig dc;
  dc.base_mints = {mint::WSOL, mint::USDC};
  std::vector<PoolDef> defs;
  for (const char* list : {"raydium_v4", "whirlpool"}) {
    simdjson::dom::array a = root[list];
    for (auto e : a) {
      const FixtureAccount* fa = fixture(std::string(std::string_view(e)));
      if (!fa || !fa->exists) continue;
      auto d = state::pool_def_from_account(fa->raw, dc);
      if (!d) continue;
      store.add_pool(*d);
      defs.push_back(*d);
    }
  }
  simdjson::dom::array rs = root["kamino_reserves"];
  for (auto e : rs) store.add_reserve(parse_pubkey(std::string_view(e)));
  for (const Pubkey& k : store.subscribed_keys()) {
    const FixtureAccount* fa = fixture(b58(k));
    if (!fa) continue;
    AccountUpdate u;
    u.key = k;
    u.slot = slot;
    if (!fa->exists) u.missing = true;
    else { u.owner = fa->raw.owner; u.data = fa->raw.data.data(); u.len = fa->raw.data.size(); }
    store.apply(u);
  }
  return defs;
}

}  // namespace

TEST(engine_finds_an_injected_cycle_on_fixture_pools) {
  StateStore store;
  auto defs = load_fixture_pools(store, 1000);
  std::vector<double> tvl(defs.size(), 1.0);
  GraphConfig g;
  g.base_mints = {mint::WSOL, mint::USDC};
  CycleIndex idx;
  idx.build(defs, tvl, g);
  size_t two = 0, three = 0;
  for (auto& c : idx.all()) (c.n == 2 ? two : three)++;
  std::printf("    fixture graph: %zu pools, %zu 2-hop and %zu 3-hop cycles\n", defs.size(), two, three);
  CHECK(two > 0);
  // Pick a 2-hop wSOL cycle with quotable legs, at least one of them Raydium v4 (whose quote reads the
  // vault balances the injection below changes).
  const Cycle* pick = nullptr;
  int ray_leg = -1;
  for (auto& c : idx.all()) {
    if (c.n != 2 || c.base != mint::WSOL) continue;
    auto a = store.pool(c.legs[0].pool), b = store.pool(c.legs[1].pool);
    if (!a->quotable(2) || !b->quotable(2)) continue;
    // Both legs must quote (adaptive-fee Whirlpools are refused by the static-fee quoter).
    if (!quote_chain(c, Snapshots{a, b, nullptr}, both_quoters(), 10'000'000).ok) continue;
    int leg = a->kind == DexKind::RaydiumV4 ? 0 : b->kind == DexKind::RaydiumV4 ? 1 : -1;
    if (leg < 0) continue;
    pick = &c;
    ray_leg = leg;
    if (a->kind == b->kind) break;  // prefer Raydium-Raydium
  }
  CHECK(pick != nullptr);
  if (!pick) return;
  std::printf("    picked cycle %u: %s -> %s, injecting on leg %d\n", pick->id, dex_name(store.pool(pick->legs[0].pool)->kind),
              dex_name(store.pool(pick->legs[1].pool)->kind), ray_leg + 1);

  TipPolicy tips;
  EngineConfig ec;
  ec.flash = true;
  ec.max_profit_ppm = 1'000'000;  // the 3% injection below is far above the 2% plausibility cap
  Engine eng(store, idx, both_quoters(), ec, tips);
  // Flash reserve for wSOL from the fixture's Kamino reserve.
  bool have_reserve = false;
  flash::FlashReserve wsol_reserve;
  std::string body = read_file(fixture_path("pools.json"));
  simdjson::dom::parser jp;
  simdjson::padded_string jps(body);
  simdjson::dom::array rs = jp.parse(jps)["kamino_reserves"];
  for (auto e : rs) {
    Pubkey rk = parse_pubkey(std::string_view(e));
    auto r = store.reserve(rk);
    if (r && r->liquidity_mint == mint::WSOL) {
      wsol_reserve = flash::make_reserve(rk, *r, 1000);
      eng.set_flash_reserve(mint::WSOL, wsol_reserve);
      have_reserve = r->flash_enabled();
    }
  }
  CHECK(have_reserve);
  // Untouched mainnet state: no candidate on this cycle (a fixture snapshot is not an open arb).
  CHECK(!eng.evaluate(pick->id, 1000));

  // Inject: the Raydium leg's output vault gains 3%, so that leg pays more out.
  const Leg& l2 = pick->legs[ray_leg];
  const PoolDef& d2 = store.def(l2.pool);
  const Pubkey& out_vault = l2.a_to_b ? d2.vault_b : d2.vault_a;
  const FixtureAccount* va = fixture(b58(out_vault));
  CHECK(va && va->exists);
  std::vector<uint8_t> bytes = va->raw.data;
  uint64_t amt;
  std::memcpy(&amt, bytes.data() + 64, 8);
  amt += amt / 100 * 3;
  std::memcpy(bytes.data() + 64, &amt, 8);
  // Re-push the pool and both vaults at the new slot so the snapshot stays internally consistent.
  for (const Pubkey& k : {d2.address, d2.vault_a, d2.vault_b}) {
    const FixtureAccount* fa = fixture(b58(k));
    AccountUpdate u{k, fa->raw.owner, k == out_vault ? bytes.data() : fa->raw.data.data(),
                    k == out_vault ? bytes.size() : fa->raw.data.size(), 1001, 0, false, false};
    store.apply(u);
  }
  std::vector<Opportunity> sent;
  std::vector<Reason> skipped;
  eng.on_send([&](const Opportunity& o) { sent.push_back(o); });
  eng.on_skip([&](const Opportunity&, Reason r) { skipped.push_back(r); });
  Opportunity o;
  bool got = eng.evaluate(pick->id, 1001, &o);
  if (!got && !skipped.empty()) std::printf("    skipped: %s\n", reason_name(skipped.back()));
  CHECK(got);
  if (!got) return;
  {
    // The same edge under the default 2% cap is skipped as implausible.
    EngineConfig capped = ec;
    capped.max_profit_ppm = EngineConfig{}.max_profit_ppm;
    Engine engc(store, idx, both_quoters(), capped, tips);
    engc.set_flash_reserve(mint::WSOL, wsol_reserve);
    std::vector<Reason> why;
    engc.on_skip([&](const Opportunity&, Reason r) { why.push_back(r); });
    if (o.profit_base * 1'000'000 > int64_t(o.x) * int64_t(capped.max_profit_ppm)) {
      CHECK(!engc.evaluate(pick->id, 1001));
      CHECK(!why.empty() && why.back() == Reason::implausible_profit);
      CHECK_EQ(engc.counters().implausible, uint64_t(1));
    }
    // Quarantine: three bad simulations on a leg stop the cycle; it returns after the quarantine.
    Engine engq(store, idx, both_quoters(), ec, tips);
    engq.set_flash_reserve(mint::WSOL, wsol_reserve);
    int64_t now = wall_ms();
    for (int k = 0; k < 3; ++k) engq.note_sim(pick->legs[0].pool, pick->legs[0].a_to_b, true, now);
    CHECK_EQ(engq.quarantined(now).size(), size_t(1));
    CHECK(!engq.evaluate(pick->id, 1001));
    CHECK_EQ(engq.counters().quarantined, uint64_t(1));
    CHECK(engq.quarantined(now + 2'000'000).empty());
  }
  std::printf("    candidate x=%llu out=%llu profit=%lld tip=%llu net=%lld ev=%lld min_out=%llu\n",
              (unsigned long long)o.x, (unsigned long long)o.final_out, (long long)o.profit_base,
              (unsigned long long)o.pr.tip_lamports, (long long)o.pr.net_lamports, (long long)o.pr.ev_lamports,
              (unsigned long long)o.pr.min_out);
  CHECK(o.pr.min_out <= o.final_out);
  CHECK(o.pr.min_out >= o.x + o.flash_fee + o.pr.tip_lamports + ec.costs.min_net_lamports);
  CHECK(o.pr.net_lamports >= int64_t(ec.costs.min_net_lamports));
  CHECK(o.x >= ec.min_trade_lamports);
  eng.dispatch_best(1001);
  CHECK_EQ(sent.size(), size_t(1));
  // Cooldown: the same cycle is not re-evaluated in the next two slots.
  CHECK(!eng.evaluate(pick->id, 1002));
  CHECK(eng.counters().cooling >= 1);
  // Own-capital mode with no balance: x_max is 0, nothing is sized.
  EngineConfig own = ec;
  own.flash = false;
  Engine eng2(store, idx, both_quoters(), own, tips);
  CHECK(!eng2.evaluate(pick->id, 1001));
  eng2.set_own_balance(mint::WSOL, 1'000'000'000'000ULL);
  CHECK(eng2.evaluate(pick->id, 1001));

  // Spread-stale: only the out vault is written again, 9 slots after the pool account. The engine refuses the
  // cycle (stale by spread) but shadow-prices it: the same edge shows up in the spread counters and examples.
  {
    const FixtureAccount* fa = fixture(b58(out_vault));
    AccountUpdate u{out_vault, fa->raw.owner, bytes.data(), bytes.size(), 1010, 0, false, false};
    store.apply(u);
  }
  Engine eng3(store, idx, both_quoters(), ec, tips);
  eng3.set_flash_reserve(mint::WSOL, wsol_reserve);
  std::vector<Opportunity> sent3;
  eng3.on_send([&](const Opportunity& o3) { sent3.push_back(o3); });
  CHECK(!eng3.evaluate(pick->id, 1010));
  eng3.dispatch_best(1010);
  CHECK(sent3.empty());
  const EngineCounters& n3 = eng3.counters();
  CHECK_EQ(n3.stale, uint64_t(1));
  CHECK_EQ(n3.stale_spread, uint64_t(1));
  CHECK_EQ(n3.stale_incomplete + n3.stale_resync + n3.stale_lag, uint64_t(0));
  CHECK_EQ(n3.spread_edge, uint64_t(1));
  CHECK_EQ(n3.spread_priced, uint64_t(1));
  CHECK(n3.spread_gross_lamports > 0);
  CHECK_EQ(n3.candidates, uint64_t(0));
  auto ex = eng3.take_spread_examples(5);
  CHECK_EQ(ex.size(), size_t(1));
  if (!ex.empty()) {
    CHECK_EQ(ex[0].pool, l2.pool);
    CHECK_EQ(ex[0].priced, uint64_t(1));
    CHECK_EQ(ex[0].best_cycle, pick->id);
  }
  CHECK(eng3.take_spread_examples(5).empty());
  // Far behind the slot clock counts as lag, not spread.
  CHECK(!eng3.evaluate(pick->id, 1010 + ec.max_slot_lag + 100));
  CHECK_EQ(eng3.counters().stale_lag, uint64_t(1));

  // No room: with min_trade above any x_max the sizer is skipped, and the cycle is not re-priced until a leg's
  // pool changes (re-pushing the pool and vaults bumps the snapshot seq).
  auto push_all = [&](uint64_t slot) {
    for (const Pubkey& k : {d2.address, d2.vault_a, d2.vault_b}) {
      const FixtureAccount* fa = fixture(b58(k));
      AccountUpdate u{k, fa->raw.owner, k == out_vault ? bytes.data() : fa->raw.data.data(),
                      k == out_vault ? bytes.size() : fa->raw.data.size(), slot, 0, false, false};
      store.apply(u);
    }
  };
  push_all(1020);
  EngineConfig huge = ec;
  huge.min_trade_lamports = 1'000'000'000'000'000ULL;
  Engine eng4(store, idx, both_quoters(), huge, tips);
  eng4.set_flash_reserve(mint::WSOL, wsol_reserve);
  CHECK(!eng4.evaluate(pick->id, 1020));
  CHECK_EQ(eng4.sizer_diag().no_room, uint64_t(1));
  CHECK_EQ(eng4.counters().sized, uint64_t(0));  // the sizer never ran
  CHECK(!eng4.evaluate(pick->id, 1020));
  CHECK_EQ(eng4.counters().no_room_memo, uint64_t(1));
  CHECK_EQ(eng4.sizer_diag().no_room, uint64_t(1));
  push_all(1021);
  CHECK(!eng4.evaluate(pick->id, 1021));
  CHECK_EQ(eng4.sizer_diag().no_room, uint64_t(2));  // re-priced after the pool changed
}

TEST(cycle_cap_lists_per_pool_hub_does_not_block) {
  // A hub pool H (B/X) on every cycle, and many spoke pools. With a cap of 2, H's list fills at once, but
  // cycles through H stay reachable from their other legs.
  Pubkey B = mint::WSOL, X = key_n(1);
  std::vector<PoolDef> pools = {def(10, B, X)};  // hub
  for (uint8_t i = 0; i < 6; ++i) pools.push_back(def(uint8_t(20 + i), X, B));
  std::vector<double> tvl = {1000, 60, 50, 40, 30, 20, 10};
  GraphConfig g;
  g.base_mints = {B};
  g.max_hops = 2;
  g.max_cycles_per_pool = 2;
  CycleIndex idx;
  idx.build(pools, tvl, g);
  CHECK(idx.cycles_of(0).size() <= 2);  // the hub re-prices at most its 2 deepest cycles
  size_t spokes_with_cycles = 0;
  for (uint32_t p = 1; p < pools.size(); ++p) spokes_with_cycles += idx.cycles_of(p).empty() ? 0 : 1;
  CHECK_EQ(spokes_with_cycles, size_t(6));  // every spoke still evaluates its cycles with the hub
  CHECK(idx.size() >= 12);                  // hub->spoke and spoke->hub for every spoke
}

TEST(sizer_log_scan_finds_small_optimum_in_huge_range) {
  Pubkey B = mint::WSOL, X = key_n(1);
  Snapshots s{ray(0, B, X, 1000'000'000'000ULL, 102'000'000'000ULL), ray(1, B, X, 1000'000'000'000ULL, 100'000'000'000ULL)};
  Cycle c = two_hop(0, true, 1, false, B, X);
  FlashFeeFn fee;
  SizerConfig sc;
  sc.min_trade = 1'000'000;
  sc.x_max = 1'000'000'000'000'000ULL;  // a million SOL: the optimum (~3.69 SOL) sits near the bottom of the range
  sc.use_closed_form = false;
  SizeResult r = size_cycle(c, s, both_quoters(), fee, sc);
  std::printf("    log scan x=%llu profit=%lld evals=%d\n", (unsigned long long)r.x, (long long)r.profit, r.evals);
  CHECK(r.ok && r.evals <= 24);
  CHECK(r.profit >= 27'399'925LL * 99 / 100);  // the brute-force optimum is 27,399,925 (sizer test above)
}

TEST(whirlpool_depth_from_active_liquidity_and_cp_base_price) {
  StateStore store;
  load_fixture_pools(store, 1000);
  size_t checked = 0;
  for (uint32_t i = 0; i < store.pool_count(); ++i) {
    auto p = store.pool(i);
    if (p->kind != DexKind::Whirlpool || !p->complete || p->wp.liquidity == 0) continue;
    for (bool d : {true, false}) {
      uint64_t depth = dex::input_depth(*p, d);
      CHECK(depth > 0);
      // A stranded copy (1/1000 of the liquidity, same vaults) gets about 1/1000 of the depth.
      PoolState stranded = *p;
      stranded.wp.liquidity = p->wp.liquidity / 1000;
      uint64_t sd = dex::input_depth(stranded, d);
      CHECK(sd <= depth / 999 + 1);
    }
    ++checked;
  }
  std::printf("    whirlpools checked: %zu\n", checked);
  CHECK(checked >= 10);
  // USDC priced from constant-product pools: lamports per USDC unit lands in a sane range ($5..$5,000 per SOL).
  std::vector<PoolDef> defs;
  for (uint32_t i = 0; i < store.pool_count(); ++i) defs.push_back(store.def(i));
  CycleIndex idx;
  GraphConfig g;
  g.base_mints = {mint::WSOL, mint::USDC};
  idx.build(defs, {}, g);
  TipPolicy tips;
  Engine eng(store, idx, both_quoters(), EngineConfig{}, tips);
  CHECK(eng.refresh_base_price_from_pools(mint::USDC));
  const BasePrice* bp = eng.base_price(mint::USDC);
  CHECK(bp != nullptr);
  double lamports_per_unit = double(bp->lamports_per_unit_x64) / 18446744073709551616.0;
  std::printf("    USDC: %.3f lamports per unit (%.2f USDC per SOL)\n", lamports_per_unit, 1e3 / lamports_per_unit);
  CHECK(lamports_per_unit > 0.2 && lamports_per_unit < 200);
  CHECK(bp->from_lamports_ceil(1'000'000) < 1'000'000'000ULL);  // min_trade in USDC units is finite
}

TEST(time_limits_follow_slot_duration) {
  Config c;  // 200 ms slots (Solana since October 2026)
  CHECK_EQ(c.chain.slot_ms, 200);
  EngineConfig e = engine_config(c);
  CHECK_EQ(e.dispatch.max_send_lag_slots, 2);  // 400 ms
  CHECK_EQ(e.dispatch.cooldown_slots, 4);      // 800 ms
  CHECK_EQ(e.max_slot_lag, uint64_t(600));     // 2 x 60 s
  c.chain.slot_ms = 400;                       // the old slot time gives back the old slot counts
  e = engine_config(c);
  CHECK(e.dispatch.max_send_lag_slots == 1 && e.dispatch.cooldown_slots == 2 && e.max_slot_lag == 300);
  CHECK_EQ(ms_to_slots(c, 1), uint64_t(1));
  CHECK_EQ(ms_to_slots(c, 0), uint64_t(1));
}

TEST(dex_dispatch_names_programs_and_unsupported_kinds) {
  for (DexKind k : {DexKind::RaydiumV4, DexKind::Whirlpool, DexKind::Cpmm, DexKind::Dlmm, DexKind::Clmm, DexKind::PumpSwap}) {
    auto back = dex_kind_from_name(dex_name(k));
    CHECK(back.has_value() && *back == k);
  }
  CHECK(!dex_kind_from_name("orca").has_value());
  CHECK(dex_program(DexKind::RaydiumV4) && *dex_program(DexKind::RaydiumV4) == prog::RAYDIUM_V4);
  CHECK(dex_program(DexKind::Whirlpool) && *dex_program(DexKind::Whirlpool) == prog::WHIRLPOOL);
  CHECK(is_constant_product(DexKind::RaydiumV4) && is_constant_product(DexKind::Cpmm) && is_constant_product(DexKind::PumpSwap));
  CHECK(!is_constant_product(DexKind::Whirlpool) && !is_constant_product(DexKind::Clmm) && !is_constant_product(DexKind::Dlmm));
  // Registering a kind with no decoder fails loudly instead of being treated as a Whirlpool.
  for (DexKind k : {DexKind::Cpmm, DexKind::Dlmm, DexKind::Clmm, DexKind::PumpSwap}) {
    if (dex_program(k)) continue;  // supported by now: covered by its own tests
    StateStore store;
    PoolDef d;
    d.kind = k;
    bool threw = false;
    try {
      store.add_pool(d);
    } catch (const std::invalid_argument&) {
      threw = true;
    }
    CHECK(threw);
    CHECK_EQ(store.pool_count(), uint32_t(0));
  }
}

TEST(bundle_sim_builds_direct_legs_and_scores_outcomes) {
  StateStore store;
  auto defs = load_fixture_pools(store, 1000);
  std::vector<double> tvl(defs.size(), 1.0);
  GraphConfig g;
  g.base_mints = {mint::WSOL, mint::USDC};
  CycleIndex idx;
  idx.build(defs, tvl, g);
  // A quotable 2-hop wSOL cycle (any DEX pair the fixtures have).
  const Cycle* pick = nullptr;
  ChainQuote ch;
  for (auto& c : idx.all()) {
    if (c.n != 2 || c.base != mint::WSOL) continue;
    auto a = store.pool(c.legs[0].pool), b = store.pool(c.legs[1].pool);
    if (!a->quotable(2) || !b->quotable(2)) continue;
    ch = quote_chain(c, Snapshots{a, b, nullptr}, both_quoters(), 10'000'000);
    if (!ch.ok) continue;
    pick = &c;
    break;
  }
  CHECK(pick != nullptr);
  if (!pick) return;
  std::shared_ptr<const PoolState> legs[2] = {store.pool(pick->legs[0].pool), store.pool(pick->legs[1].pool)};
  Opportunity o;
  o.cycle = pick->id;
  o.x = 10'000'000;
  o.leg_out[0] = ch.outs[0];
  o.leg_out[1] = ch.outs[1];
  o.final_out = ch.final_out;
  Pubkey holder = parse_pubkey("9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM");
  tx::IxList l;
  auto lay = app::build_sim_ixs(l, *pick, std::span<const std::shared_ptr<const PoolState>>(legs, 2), o, holder, {true});
  CHECK(lay.has_value());
  if (!lay) return;
  CHECK_EQ(int(l.n), 4);  // ATA create for the intermediate, then two swaps
  CHECK_EQ(int(lay->first_swap), 2);
  CHECK(l.ixs[0].program == prog::COMPUTE_BUDGET && l.ixs[1].program == prog::ATA);
  CHECK(l.ixs[1].accounts[1].key == lay->intermediates[0]);
  CHECK(l.ixs[2].program == *dex_program(legs[0]->kind) && l.ixs[3].program == *dex_program(legs[1]->kind));
  // Amount in: after Raydium's 1-byte tag, after Whirlpool's 8-byte discriminator.
  auto amount_at = [](const tx::Ix& ix, DexKind k) {
    uint64_t v;
    std::memcpy(&v, ix.data + (k == DexKind::RaydiumV4 ? 1 : 8), 8);
    return v;
  };
  uint64_t in1 = amount_at(l.ixs[2], legs[0]->kind), in2 = amount_at(l.ixs[3], legs[1]->kind);
  CHECK_EQ(in1, o.x);
  CHECK_EQ(in2, o.leg_out[0]);  // leg 2 takes leg 1's quoted output
  CHECK(lay->base_account == ata(holder, mint::WSOL, prog::TOKEN));
  CHECK(!app::unsigned_tx_b64(holder, l).empty());  // fits 1,232 bytes without lookup tables
  // No ATA create when the holder already has the intermediate account.
  auto lay2 = app::build_sim_ixs(l, *pick, std::span<const std::shared_ptr<const PoolState>>(legs, 2), o, holder, {false});
  CHECK(lay2 && lay2->first_swap == 1 && l.n == 3);

  CHECK(app::classify_sim_error("{\"InstructionError\":[2,{\"Custom\":30}]}", 1, 2) == obs::SimFail::leg2);
  CHECK(app::classify_sim_error("{\"InstructionError\":[1,\"InvalidAccountData\"]}", 1, 2) == obs::SimFail::leg1);
  CHECK(app::classify_sim_error("{\"InstructionError\":[0,\"Custom\"]}", 1, 2) == obs::SimFail::setup);
  CHECK(app::classify_sim_error("\"AccountNotFound\"", 1, 2) == obs::SimFail::other);

  // Scoring: simulated final out 500 above x + flash fee; the candidate valued 1,000 base units at 2,000 lamports.
  app::SimJob j;
  j.o = o;
  j.o.x = 1'000'000;
  j.o.flash_fee = 10;
  j.o.profit_base = 1000;
  j.o.final_out = 1'001'010;
  j.c = *pick;
  j.cand.slot = 77;
  j.cand.gross_lamports = 2000;
  j.cand.tip_lamports = 300;
  j.cand.fee_lamports = 5000;
  j.cand.signature[0] = 0xab;
  auto r = app::sim_outcome(j, true, obs::SimFail::none, 80, 1'000'510, 123'456, 5);
  CHECK_EQ(int(r.kind), int(obs::RecordKind::Outcome));
  CHECK_EQ(int(r.outcome), int(obs::Outcome::SimulatedOk));
  CHECK_EQ(r.profit_base, int64_t(500));
  CHECK_EQ(r.gross_lamports, uint64_t(1000));
  CHECK_EQ(r.net_lamports, int64_t(1000 - 300 - 5000));
  CHECK_EQ(r.min_out, uint64_t(1'001'010));
  CHECK_EQ(r.slot, uint64_t(80));
  CHECK_EQ(r.state_slot[0], uint64_t(77));
  CHECK_EQ(r.cu_limit, uint32_t(123'456));
  CHECK_EQ(int(r.signature[0]), 0xab);
  auto f = app::sim_outcome(j, false, obs::SimFail::leg2, 81, 0, 9, 6);
  CHECK_EQ(int(f.outcome), int(obs::Outcome::SimulationFailed));
  CHECK_EQ(int(f.reason), int(obs::SimFail::leg2));
  CHECK_EQ(f.net_lamports, int64_t(0));

  // Outcome records sit in the same log; the replay report reads candidates only.
  std::string path = std::string(ARB_SOURCE_DIR) + "/build/test-sim-outcome.bin";
  {
    obs::DecisionLog log("", 0);
    CHECK(log.open_path(path));
    obs::DecisionRecord cand = j.cand;
    CHECK(log.write(cand));
    CHECK(log.write(r));
    log.close();
  }
  auto recs = obs::read_records({path});
  CHECK_EQ(recs.size(), size_t(2));
  CHECK_EQ(obs::aggregate(recs, 0, 0).records, uint64_t(1));
  std::remove(path.c_str());
}

TEST(pumpswap_addresses_and_fee_tiers) {
  // Published addresses (pump-public-docs PUMP_SWAP_README.md, PUMP_SWAP_TRADE_V2.md).
  CHECK(tx::dexix::pump_global_config() == parse_pubkey("ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw"));
  CHECK(tx::dexix::pump_fee_config() == parse_pubkey("5PHirr8joyTMp9JMm6nW7hNDVyEYdkzDqazxPD7RaTjx"));
  const uint8_t index0[2] = {0, 0};
  Pubkey pool = pda({std::string_view("pool"), Seed(index0, 2), parse_pubkey("9XDYTfQKwW8sHPqnFdUreMmtmffmkHVPGTNV2e3LKxNW"),
                     parse_pubkey("7LSsEoJGhLeZzGvDofTdNg7M3JttxQqGWNLo6vWMpump"), mint::WSOL},
                    prog::PUMPSWAP);
  CHECK(pool == parse_pubkey("GseMAnNDvntR5uFePZ51yZBXzNSn7GdFPkfHwfr6d77J"));

  // Fee selection (pump-rust-client fees.rs selection_matrix): tiers by market cap for pump pools, flat otherwise.
  auto cfg = std::make_shared<state::PumpFeeConfig>();
  cfg->flat_fees = {25, 5, 0};
  cfg->fee_tiers = {{0, {1, 90, 30}}, {1000, {2, 80, 20}}};
  cfg->stable_fee_tiers = {{0, {3, 70, 10}}, {1000, {4, 60, 5}}};
  auto mint_info = std::make_shared<state::Mint>();
  mint_info->supply = 1000;
  PoolState p;
  p.kind = DexKind::PumpSwap;
  p.mint_a_info = mint_info;
  p.mint_b = mint::WSOL;
  p.pump_fees = cfg;
  p.pump_canonical = true;
  auto f = dex::pump_fee_bps(p, 1000, 999);  // market cap 999 < 1000: first tier
  CHECK(f && f->lp == 1 && f->protocol == 90 && f->creator == 30);
  f = dex::pump_fee_bps(p, 1000, 1000);  // 1000: second tier
  CHECK(f && f->lp == 2 && f->protocol == 80 && f->creator == 20);
  p.mint_b = mint::USDC;
  f = dex::pump_fee_bps(p, 1000, 999);
  CHECK(f && f->lp == 3 && f->protocol == 70);
  p.pump_canonical = false;  // not a pump-migrated pool: flat fees
  f = dex::pump_fee_bps(p, 1000, 5000);
  CHECK(f && f->lp == 25 && f->protocol == 5 && f->creator == 0);
  // The pool's own creator fee wins when the global gate is on.
  p.pump_canonical = true;
  p.mint_b = mint::WSOL;
  p.pump.creator_fee_bps = 250;
  p.pump_cfg.creator_fee_configurable = true;
  f = dex::pump_fee_bps(p, 1000, 999);
  CHECK(f && f->creator == 250);
}

TEST(clmm_tick_math_matches_program_bounds_and_round_trips) {
  using namespace dex::clmm;
  CHECK(*sqrt_price_at_tick(0) == (u128(1) << 64));
  CHECK(*sqrt_price_at_tick(MIN_TICK) == MIN_SQRT_PRICE_X64);
  CHECK(*sqrt_price_at_tick(MAX_TICK) == MAX_SQRT_PRICE_X64);
  CHECK(!sqrt_price_at_tick(MAX_TICK + 1).has_value());
  CHECK(*tick_at_sqrt_price(MIN_SQRT_PRICE_X64) == MIN_TICK);
  int bad = 0;
  for (int32_t t : {-443635, -200000, -88722, -1001, -60, -1, 1, 59, 60, 1000, 88722, 200000, 443635}) {
    u128 s = *sqrt_price_at_tick(t);
    if (*tick_at_sqrt_price(s) != t) ++bad;
    if (*tick_at_sqrt_price(s + 1) != t) ++bad;   // inside [tick, tick+1)
    if (*tick_at_sqrt_price(s - 1) != t - 1) ++bad;
  }
  CHECK_EQ(bad, 0);
  // Bitmap navigation: arrays at starts -60 and 120 (spacing 1, 60 ticks per array).
  uint64_t bm[16]{};
  auto set = [&](int32_t start) {
    int32_t pos = start / 60 + 512;
    bm[pos / 64] |= uint64_t(1) << (pos % 64);
  };
  set(-60);
  set(120);
  int32_t start = 0, out = 0;
  CHECK(!current_array_initialized(bm, 5, 1, start) && start == 0);
  CHECK(next_initialized_array(bm, 0, 1, true, out) && out == -60);
  CHECK(next_initialized_array(bm, 0, 1, false, out) && out == 120);
  CHECK(current_array_initialized(bm, -1, 1, start) && start == -60);
  // One exact-in step that stays inside the range: fee on input, zero for one.
  auto r = compute_swap_exact_in(u128(1) << 64, *sqrt_price_at_tick(-100), u128(1'000'000'000'000ULL), 1'000'000, 2500, true, true);
  CHECK(r.has_value() && r->amount_in + r->fee_amount == 1'000'000 && r->amount_out > 0 && r->amount_out < 1'000'000);
}

TEST(cp_acceptance_transfer_fee_extension_even_zero) {
  dex::CpmmQuoter cq; dex::PumpSwapQuoter pq;
  for (DexKind kind : {DexKind::Cpmm, DexKind::PumpSwap}) {
    PoolState p; p.kind = kind; p.complete = true;
    CHECK(kind == DexKind::Cpmm ? cq.supports(p) : pq.supports(p));
    auto m = std::make_shared<state::Mint>(); m->has_transfer_fee = true;
    for (bool side : {false, true}) {
      p.mint_a_info = side ? m : nullptr; p.mint_b_info = side ? nullptr : m;
      CHECK(!(kind == DexKind::Cpmm ? cq.supports(p) : pq.supports(p)));
      for (bool dir : {false, true}) {
        auto q = kind == DexKind::Cpmm ? cq.quote_exact_in(p, dir, 1000) : pq.quote_exact_in(p, dir, 1000);
        CHECK(!q && q.error().code == Errc::unsupported);
      }
    }
  }
}

TEST(cp_acceptance_seed_fees_directions_and_exact_final) {
  dex::CpmmQuoter cq; dex::PumpSwapQuoter pq;
  Quoters qs; qs.by_kind[uint8_t(DexKind::Cpmm)] = &cq; qs.by_kind[uint8_t(DexKind::PumpSwap)] = &pq;
  for (bool direction : {false, true}) for (uint8_t mode : {0, 1, 2}) {
    auto a = std::make_shared<PoolState>(); auto b = std::make_shared<PoolState>();
    a->kind = DexKind::Cpmm; b->kind = DexKind::PumpSwap; a->complete = b->complete = true;
    a->cpmm_cfg.trade_fee_rate = 2500; a->cpmm_cfg.creator_fee_rate = 1000;
    a->cpmm.enable_creator_fee = true; a->cpmm.creator_fee_on = mode;
    a->vault_a_amount = direction ? 1'000'000'000 : 2'000'000'000;
    a->vault_b_amount = direction ? 2'000'000'000 : 1'000'000'000;
    b->vault_a_amount = direction ? 1'100'000'000 : 2'000'000'000;
    b->vault_b_amount = direction ? 2'000'000'000 : 1'100'000'000;
    auto fees = std::make_shared<state::PumpFeeConfig>(); fees->flat_fees = {20, 5, 10}; b->pump_fees = fees;
    b->pump.coin_creator = key_n(55);
    Cycle c; c.n = 2; c.legs[0].a_to_b = direction; c.legs[1].a_to_b = !direction;
    Snapshots snaps{}; snaps[0] = a; snaps[1] = b;
    uint64_t seed = closed_form_2hop_cp(c, snaps); CHECK(seed > 0);
    SizerConfig cfg; cfg.min_trade = 1000; cfg.x_max = 100'000'000; cfg.max_evals = 48;
    auto sized = size_cycle(c, snaps, qs, FlashFeeFn{}, cfg);
    CHECK(sized.ok);
    auto exact = quote_chain(c, snaps, qs, sized.x);
    CHECK(exact.ok); CHECK_EQ(exact.final_out, sized.chain.final_out);
    CHECK_EQ(sized.profit, int64_t(exact.final_out - sized.x));
    cfg.use_closed_form = false;
    auto scan = size_cycle(c, snaps, qs, FlashFeeFn{}, cfg);
    CHECK(scan.ok); CHECK(std::abs(sized.profit - scan.profit) < 100);
    b->pump.is_cashback_coin = true; CHECK_EQ(closed_form_2hop_cp(c, snaps), uint64_t(0));
    b->pump.is_cashback_coin = false; b->pump_fees.reset(); CHECK_EQ(closed_form_2hop_cp(c, snaps), uint64_t(0));
  }
}

TEST(cp_acceptance_seed_pump_tiers_and_fallback) {
  auto a = std::make_shared<PoolState>(); auto b = std::make_shared<PoolState>();
  a->kind = b->kind = DexKind::PumpSwap; a->complete = b->complete = true;
  a->vault_a_amount = b->vault_a_amount = 1'000'000;
  a->vault_b_amount = 2'000'000; b->vault_b_amount = 1'000'000;
  auto m = std::make_shared<state::Mint>(); m->supply = 1000;
  a->mint_a_info = b->mint_a_info = m; a->mint_b = b->mint_b = mint::WSOL;
  a->pump_canonical = b->pump_canonical = true;
  auto f = std::make_shared<state::PumpFeeConfig>();
  f->fee_tiers = {{0, {10, 10, 0}}, {2000, {100, 100, 0}}};
  a->pump_fees = b->pump_fees = f;
  Cycle c; c.n = 2; c.legs[0].a_to_b = true; c.legs[1].a_to_b = false;
  Snapshots s{}; s[0] = a; s[1] = b;
  const auto high_fee = closed_form_2hop_cp(c, s); CHECK(high_fee > 0);
  f->fee_tiers[1].fees = {10, 10, 0};
  CHECK(closed_form_2hop_cp(c, s) > high_fee);
  f->fee_tiers.clear(); CHECK_EQ(closed_form_2hop_cp(c, s), uint64_t(0));
  f->flat_fees = {10, 10, 0}; a->pump_canonical = b->pump_canonical = false;
  CHECK(closed_form_2hop_cp(c, s) > 0);
  a->pump_cfg.disable_flags = 16; CHECK_EQ(closed_form_2hop_cp(c, s), uint64_t(0));
}

TEST(cp_acceptance_seed_bit_extremes_fall_back_without_truncation) {
  Cycle c; c.n = 2; c.legs[0].a_to_b = true; c.legs[1].a_to_b = false;
  auto a = std::make_shared<PoolState>(); auto b = std::make_shared<PoolState>();
  a->kind = b->kind = DexKind::RaydiumV4;
  a->complete = b->complete = true;
  a->vault_a_amount = b->vault_b_amount = UINT64_MAX / 2;
  a->vault_b_amount = b->vault_a_amount = UINT64_MAX;
  a->ray.swap_fee_denominator = b->ray.swap_fee_denominator = UINT64_MAX;
  a->ray.swap_fee_numerator = b->ray.swap_fee_numerator = 1;
  Snapshots s{}; s[0] = a; s[1] = b;
  CHECK_EQ(closed_form_2hop_cp(c, s), uint64_t(0));  // n*n and d*d cannot be Q16 scaled
  a->kind = b->kind = DexKind::Cpmm;
  a->cpmm_cfg.trade_fee_rate = b->cpmm_cfg.trade_fee_rate = 2500;
  CHECK_EQ(closed_form_2hop_cp(c, s), uint64_t(0));  // S>>16 exceeds u128
  a->vault_a_amount = b->vault_b_amount = 1'000'000;
  a->vault_b_amount = b->vault_a_amount = 2'000'000;
  CHECK(closed_form_2hop_cp(c, s) > 0);
  a->cpmm.creator_fee_on = 2; a->cpmm.enable_creator_fee = true;
  a->cpmm_cfg.creator_fee_rate = 999999; a->vault_b_amount = 1;
  CHECK_EQ(closed_form_2hop_cp(c, s), uint64_t(0));  // output reserve scales to zero
}

TEST(helius_quota_flag_and_backoff) {
  usage::set_quota(false, 0);
  CHECK(usage::is_quota_body("{\"jsonrpc\":\"2.0\",\"error\":{\"code\":-32429,\"message\":\"max usage reached\"}}"));
  CHECK(!usage::is_quota_body("Too many requests"));
  usage::record_rpc("getSlot", 429, 10, 20, "Too many requests", 1000);  // a rate limit, not the plan cap
  CHECK(!usage::quota_exhausted());
  usage::record_rpc("getSlot", 429, 10, 20, "max usage reached", 2000);
  CHECK(usage::quota_exhausted());
  CHECK_EQ(usage::quota_since_ms(), int64_t(2000));
  int64_t last = 0;
  CHECK(usage::may_call(last, 70'000));   // first call after the hold
  CHECK(!usage::may_call(last, 100'000)); // within a minute of the last one
  CHECK(usage::may_call(last, 130'001));
  usage::record_rpc("getSlot", 200, 10, 20, "", 140'000);  // a success clears it
  CHECK(!usage::quota_exhausted());
  CHECK(usage::may_call(last, 140'001));
  bool found = false;
  for (auto& [m, s] : usage::rpc_snapshot())
    if (m == "getSlot") found = s.calls >= 3 && s.rate_limited >= 2;
  CHECK(found);
}
