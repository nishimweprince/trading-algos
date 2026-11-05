// Phase 1: frame parse, recorder round trip, StateStore snapshots, subscription plan, discovery.
#include <unistd.h>

#include <cstdio>
#include <ctime>
#include <set>

#include "core/programs.hpp"
#include "feed/feed_handler.hpp"
#include "obs/latency.hpp"
#include "feed/frames.hpp"
#include "feed/resync.hpp"
#include "feed/subscription.hpp"
#include "feed/update.hpp"
#include "fixture.hpp"
#include "core/pb.hpp"
#include "state/discovery.hpp"
#include "test.hpp"

using namespace arb;
using namespace arbtest;

namespace {

std::string pools_json() { return read_file(fixture_path("pools.json")); }

std::vector<std::string> pool_list(const char* name) {
  static simdjson::dom::parser p;
  static simdjson::padded_string ps = simdjson::padded_string(pools_json());
  static simdjson::dom::element root = p.parse(ps);
  std::vector<std::string> out;
  simdjson::dom::array a = root[name];
  for (auto e : a) out.emplace_back(std::string_view(e));
  return out;
}

state::DiscoveryConfig discovery_cfg() {
  state::DiscoveryConfig c;
  c.base_mints = {mint::WSOL, mint::USDC};
  c.safety.freeze_allowlist = {mint::USDC, mint::USDT};
  c.min_pool_tvl_sol = 50;
  return c;
}

}  // namespace

TEST(frame_parse_account_and_slot) {
  Pubkey key = mint::USDC, owner = prog::TOKEN;
  uint8_t data[5] = {1, 2, 3, 4, 5};
  std::string f = feed::encode_account_frame(key, owner, data, 5, 1234, 99, 7);
  feed::UpdateView v;
  CHECK(feed::parse_update(reinterpret_cast<const uint8_t*>(f.data()), f.size(), v));
  CHECK(v.kind == feed::UpdateView::Account);
  CHECK_EQ(v.n_filters, uint8_t(1));
  CHECK(v.filters[0] == "accounts");
  CHECK(v.account.pubkey.size() == 32 && key_eq(reinterpret_cast<const uint8_t*>(v.account.pubkey.data()), key));
  CHECK(key_eq(reinterpret_cast<const uint8_t*>(v.account.owner.data()), owner));
  CHECK_EQ(v.account.data.size(), size_t(5));
  CHECK_EQ(v.account.lamports, uint64_t(1234));
  CHECK_EQ(v.account.slot, uint64_t(99));
  CHECK_EQ(v.account.write_version, uint64_t(7));
  std::string s = feed::encode_slot_frame(12345, 1);
  CHECK(feed::parse_update(reinterpret_cast<const uint8_t*>(s.data()), s.size(), v));
  CHECK(v.kind == feed::UpdateView::Slot);
  CHECK_EQ(v.slot.slot, uint64_t(12345));
  CHECK_EQ(v.slot.status, uint32_t(1));
  // Truncated frames are rejected, not misread.
  CHECK(!feed::parse_update(reinterpret_cast<const uint8_t*>(f.data()), f.size() - 3, v));
}

TEST(subscribe_request_encoding) {
  feed::ShardSpec spec;
  spec.accounts = {mint::USDC, mint::WSOL};
  spec.from_slot = 777;
  std::string req = feed::encode_subscribe_request(spec);
  // Walk the top-level fields: accounts map (1), slots map (2), commitment (6) = 0, from_slot (11) = 777.
  pb::Reader r(reinterpret_cast<const uint8_t*>(req.data()), req.size());
  uint32_t f, wt;
  std::set<uint32_t> seen;
  uint64_t commitment = 99, from = 0;
  int account_strings = 0;
  while (r.next(f, wt)) {
    seen.insert(f);
    if (f == 1) {
      pb::Reader e(r.bytes());
      while (e.next(f, wt)) {
        if (f == 2) {
          pb::Reader filt(e.bytes());
          while (filt.next(f, wt)) {
            if (f == 2) { std::string_view k = filt.bytes(); Pubkey pkk; CHECK(b58_decode32(k, pkk)); ++account_strings; }
            else filt.skip(wt);
          }
        } else e.skip(wt);
      }
    } else if (f == 6) commitment = r.varint();
    else if (f == 11) from = r.varint();
    else r.skip(wt);
  }
  CHECK(r.ok);
  CHECK(seen.count(1) && seen.count(2) && seen.count(6) && seen.count(11));
  CHECK_EQ(commitment, uint64_t(0));
  CHECK_EQ(from, uint64_t(777));
  CHECK_EQ(account_strings, 2);
}

TEST(subscription_plan_shards_and_diff) {
  std::vector<Pubkey> keys;
  for (int i = 0; i < 1000; ++i) {
    Pubkey k{};
    k[0] = uint8_t(i & 0xff);
    k[1] = uint8_t(i >> 8);
    keys.push_back(k);
  }
  auto p = feed::plan_subscriptions(keys, {mint::WSOL}, 4, 300);
  CHECK_EQ(p.pool_shards.size(), size_t(4));
  size_t total = 0;
  for (auto& s : p.pool_shards) {
    CHECK(s.accounts.size() <= 300);
    total += s.accounts.size();
  }
  CHECK_EQ(total, size_t(1000));
  CHECK_EQ(p.overflow, size_t(0));
  CHECK(p.pool_shards[0].slots && !p.pool_shards[1].slots);
  auto p2 = feed::plan_subscriptions(keys, {}, 3, 300);
  CHECK_EQ(p2.overflow, size_t(100));
  std::vector<Pubkey> after(keys.begin() + 10, keys.end());
  after.push_back(mint::USDC);
  auto d = feed::diff_keys(keys, after);
  CHECK_EQ(d.added.size(), size_t(1));
  CHECK_EQ(d.removed.size(), size_t(10));
}

TEST(recorder_round_trip_and_replay_into_store) {
  // Write fixture accounts of two pools as frames, read them back, replay into a StateStore.
  char path[] = "/tmp/arb_frames_XXXXXX";
  int fd = mkstemp(path);
  CHECK(fd >= 0);
  close(fd);
  auto rays = pool_list("raydium_v4");
  auto wps = pool_list("whirlpool");
  auto cfg = discovery_cfg();
  StateStore store;
  std::vector<uint32_t> ids;
  std::vector<Pubkey> keys;
  for (auto& addr : {rays[0], wps[0]}) {
    const FixtureAccount* a = fixture(addr);
    auto def = state::pool_def_from_account(a->raw, cfg);
    CHECK(def.has_value());
    ids.push_back(store.add_pool(*def));
  }
  keys = store.subscribed_keys();
  {
    feed::FrameWriter w("", 3600);
    CHECK(w.open_path(path));
    uint64_t wv = 1;
    std::string s = feed::encode_slot_frame(1000);
    CHECK(w.write(reinterpret_cast<const uint8_t*>(s.data()), uint32_t(s.size()), 1, 1));
    for (auto& k : keys) {
      const FixtureAccount* a = fixture(b58(k));
      if (!a || !a->exists) continue;
      std::string f = feed::encode_account_frame(k, a->raw.owner, a->raw.data.data(), a->raw.data.size(), a->lamports, 1000, wv++);
      CHECK(w.write(reinterpret_cast<const uint8_t*>(f.data()), uint32_t(f.size()), 2, 2));
    }
  }
  feed::FrameReader rd;
  CHECK(rd.open(path));
  feed::SlotClock clock;
  feed::FeedHandler h(store, clock);
  feed::Frame fr;
  size_t n = 0;
  while (rd.next(fr)) {
    CHECK(h.on_frame(fr.data.data(), fr.data.size(), fr.recv_ns));
    ++n;
  }
  CHECK(!rd.truncated());
  CHECK(n >= 10);
  CHECK_EQ(clock.slot.load(), uint64_t(1000));
  for (uint32_t id : ids) {
    auto s = store.pool(id);
    CHECK(s->complete);
    CHECK(s->quotable(2));
  }
  auto w = store.pool(ids[1]);
  CHECK(w->kind == DexKind::Whirlpool);
  int loaded = 0, missing_or_unknown = 0;
  for (uint8_t i = 0; i < w->n_ticks; ++i) (w->ticks[i].state == TickArraySlot::Loaded ? loaded : missing_or_unknown)++;
  CHECK(loaded >= 1);
  uint32_t changed = 0, popped = 0;
  while (store.pop_changed(changed)) ++popped;
  CHECK(popped > 0);
  // Stale updates (older slot) are ignored; a vault update at a far slot breaks quotability.
  const FixtureAccount* va = fixture(b58(store.def(ids[0]).vault_a));
  AccountUpdate u{store.def(ids[0]).vault_a, va->raw.owner, va->raw.data.data(), va->raw.data.size(), 1010, 0, false, false};
  CHECK(store.apply(u));
  CHECK(!store.pool(ids[0])->quotable(2));
  // A resync read marks the pool as awaiting a push.
  AccountUpdate r = u;
  r.slot = 1011;
  r.from_resync = true;
  store.apply(r);
  CHECK(store.pool(ids[0])->awaiting_push);
  std::remove(path);
}

TEST(decoder_rejects_garbage_frames_counted) {
  StateStore store;
  feed::SlotClock clock;
  feed::FeedHandler h(store, clock);
  uint8_t junk[7] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
  CHECK(!h.on_frame(junk, sizeof junk, 0));
  CHECK_EQ(h.bad_frames(), uint64_t(1));
}

TEST(discovery_on_recorded_gpa) {
  auto cfg = discovery_cfg();
  auto lookup = fixture_lookup();
  auto ray = state::parse_gpa_response(read_file(fixture_path("gpa_raydium.json")));
  auto wp = state::parse_gpa_response(read_file(fixture_path("gpa_whirlpool.json")));
  CHECK(ray.size() >= 5);
  CHECK(wp.size() >= 5);
  for (auto& a : ray) CHECK(a.owner == prog::RAYDIUM_V4 && a.data.size() == state::RAYDIUM_V4_LEN);
  for (auto& a : wp) CHECK(a.owner == prog::WHIRLPOOL && a.data.size() == state::WHIRLPOOL_LEN);
  // Add the fixture pools (top SOL/USDC pairs) so the graph has multi-pool mints.
  std::vector<state::RawAccount> all = ray;
  all.insert(all.end(), wp.begin(), wp.end());
  for (auto* list : {"raydium_v4", "whirlpool"})
    for (auto& addr : pool_list(list)) all.push_back(fixture(addr)->raw);
  auto res = state::discover(all, lookup, cfg);
  CHECK(res.sol_price_usdc > 10 && res.sol_price_usdc < 10000);
  CHECK(res.tracked.size() >= 10);
  for (auto& c : res.tracked) {
    CHECK(c.tvl_sol >= cfg.min_pool_tvl_sol);
    CHECK(c.def.mint_a == mint::WSOL || c.def.mint_b == mint::WSOL || c.def.mint_a == mint::USDC || c.def.mint_b == mint::USDC);
    if (c.def.kind == DexKind::Whirlpool) CHECK(c.def.tick_arrays.size() == 6);
  }
  // Every tracked non-base mint sits in >= 2 tracked-or-candidate pools; SOL and USDC each in many.
  CHECK(res.graph[mint::WSOL].size() >= 10);
  CHECK(res.graph[mint::USDC].size() >= 10);
  // Safety and structure rejections are counted by reason.
  size_t rejected = 0;
  for (auto& [why, n] : res.reject_counts) rejected += n;
  CHECK(rejected > 0);
  CHECK(res.reject_counts.count("adaptive_fee_unsupported") || res.reject_counts.count("tvl"));
  // Capping keeps the deepest pools.
  auto capped = cfg;
  capped.max_tracked_pools = 5;
  auto r2 = state::discover(all, lookup, capped);
  CHECK_EQ(r2.tracked.size(), size_t(5));
  for (size_t i = 1; i < r2.tracked.size(); ++i) CHECK(r2.tracked[i - 1].tvl_sol >= r2.tracked[i].tvl_sol);
}

TEST(token_safety_rules) {
  risk::SafetyPolicy p;
  p.freeze_allowlist = {mint::USDC};
  state::Mint m;
  m.token_program = prog::TOKEN;
  CHECK(risk::check_mint(mint::WSOL, m, p).ok);
  m.has_freeze_authority = true;
  CHECK(!risk::check_mint(mint::WSOL, m, p).ok);
  CHECK(risk::check_mint(mint::USDC, m, p).ok);
  state::Mint t;
  t.token_program = prog::TOKEN_2022;
  t.ext_mask = uint64_t(1) << state::ext::TransferHook;
  CHECK_EQ(std::string(risk::check_mint(mint::WSOL, t, p).reason), std::string("transfer_hook"));
  t.ext_mask = uint64_t(1) << state::ext::PermanentDelegate;
  CHECK_EQ(std::string(risk::check_mint(mint::WSOL, t, p).reason), std::string("permanent_delegate"));
  t.ext_mask = uint64_t(1) << state::ext::DefaultAccountState;
  t.default_account_state = 2;
  CHECK_EQ(std::string(risk::check_mint(mint::WSOL, t, p).reason), std::string("default_frozen"));
  t.ext_mask = uint64_t(1) << state::ext::TransferFeeConfig;
  t.default_account_state = 0;
  t.has_transfer_fee = true;
  t.newer_fee = {100, 1'000'000, 150};
  CHECK_EQ(std::string(risk::check_mint(mint::WSOL, t, p).reason), std::string("transfer_fee_too_high"));
  t.newer_fee.basis_points = 50;
  CHECK(risk::check_mint(mint::WSOL, t, p).ok);
  // Real fixture: PYUSD (Token-2022, transfer fee + hook + permanent delegate) is rejected.
  const FixtureAccount* py = fixture("2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo");
  auto pm = state::decode_mint(py->raw.data.data(), py->raw.data.size(), py->raw.owner);
  CHECK(pm && pm->has_transfer_fee);
  CHECK(!risk::check_mint(py->raw.key, *pm, p).ok);
  // Transfer fee math: ceil(amount * bps / 1e4) capped at maximum_fee; epoch selects older/newer.
  state::TransferFee f{0, 5000, 50};
  CHECK_EQ(state::transfer_fee(f, 1), uint64_t(1));
  CHECK_EQ(state::transfer_fee(f, 10000), uint64_t(50));
  CHECK_EQ(state::transfer_fee(f, 10001), uint64_t(51));
  CHECK_EQ(state::transfer_fee(f, 100'000'000), uint64_t(5000));
  state::Mint e;
  e.older_fee = {10, 0, 10};
  e.newer_fee = {20, 0, 20};
  CHECK_EQ(e.epoch_fee(19).basis_points, uint16_t(10));
  CHECK_EQ(e.epoch_fee(20).basis_points, uint16_t(20));
}

TEST(feed_latency_created_at_and_slot_offset) {
  // LatencyWindow: percentiles over the window; old samples expire.
  obs::LatencyWindow w(60);
  for (int i = 1; i <= 100; ++i) w.add(uint64_t(i) * 1'000'000'000ULL / 10, i);  // 0.1 s apart
  auto s = w.stats(10'000'000'000ULL);
  CHECK(s.count == 100 && s.p50 == 50 && s.p99 == 99 && s.max == 100);
  CHECK_EQ(w.stats(80'000'000'000ULL).count, uint64_t(0));  // all older than 60 s
  CHECK_EQ(w.stats(80'000'000'000ULL).p50, int64_t(-1));

  StateStore store;
  feed::SlotClock clock;
  feed::FeedHandler h(store, clock);
  // An interslot status (first shred received) marks the slot start but does not move the clock.
  std::string fs = feed::encode_slot_frame(500, 3);
  CHECK(h.on_frame(reinterpret_cast<const uint8_t*>(fs.data()), fs.size(), 1'000'000'000ULL));
  CHECK_EQ(clock.slot.load(), uint64_t(0));
  std::string pr = feed::encode_slot_frame(499, 0);
  CHECK(h.on_frame(reinterpret_cast<const uint8_t*>(pr.data()), pr.size(), 1'000'100'000ULL));
  CHECK_EQ(clock.slot.load(), uint64_t(499));
  // An account update in slot 500, 120 ms after the slot started, created by the server 30 ms before we got it.
  timespec wt, mt;
  clock_gettime(CLOCK_REALTIME, &wt);
  clock_gettime(CLOCK_MONOTONIC, &mt);
  uint64_t recv = 1'120'000'000ULL;
  int64_t wall_at_recv = (int64_t(wt.tv_sec) - int64_t(mt.tv_sec)) * 1'000'000'000LL + (int64_t(wt.tv_nsec) - int64_t(mt.tv_nsec)) + int64_t(recv);
  uint8_t data[165] = {};
  std::string f = feed::encode_account_frame(mint::USDC, prog::TOKEN, data, sizeof data, 1, 500, 1, "accounts", wall_at_recv - 30'000'000LL);
  feed::UpdateView v;
  CHECK(feed::parse_update(reinterpret_cast<const uint8_t*>(f.data()), f.size(), v));
  CHECK_EQ(v.created_at_ns, wall_at_recv - 30'000'000LL);
  h.on_frame(reinterpret_cast<const uint8_t*>(f.data()), f.size(), recv);  // an unregistered account: not applied, but timed
  auto lag = h.feed_lag_us().stats(recv);
  auto off = h.slot_offset_us().stats(recv);
  CHECK(lag.count == 1 && lag.p50 >= 29'000 && lag.p50 <= 31'000);  // ~30 ms (clock read jitter only)
  CHECK(off.count == 1 && off.p50 == 120'000);
}

TEST(resync_with_live_stream_is_quotable_at_once) {
  // A resync read with every stream already subscribed is usable at once; one without waits for a push.
  auto rays = pool_list("raydium_v4");
  auto cfg = discovery_cfg();
  for (bool live : {true, false}) {
    StateStore store;
    auto def = state::pool_def_from_account(fixture(rays[0])->raw, cfg);
    uint32_t id = store.add_pool(*def);
    std::vector<Pubkey> keys = feed::pool_ordered_keys(store, {id});
    CHECK(keys.size() >= 5 && keys[0] == def->address && keys[1] == def->vault_a && keys[2] == def->vault_b);
    for (const Pubkey& k : keys) {
      const FixtureAccount* a = fixture(b58(k));
      AccountUpdate u{k, a->raw.owner, a->raw.data.data(), a->raw.data.size(), 1000, 0, !a->exists, !live};
      store.apply(u);
    }
    CHECK_EQ(store.pool(id)->quotable(2), live);
  }
}

TEST(alpenglow_competing_banks_suspect_then_resync_losers) {
  auto rays = pool_list("raydium_v4");
  auto cfg = discovery_cfg();
  StateStore store;
  auto def = state::pool_def_from_account(fixture(rays[0])->raw, cfg);
  uint32_t id = store.add_pool(*def);
  feed::SlotClock clock;
  feed::FeedHandler h(store, clock);
  auto push = [&](const Pubkey& k, uint64_t slot, uint64_t wv, const uint64_t* bank) {
    const FixtureAccount* a = fixture(b58(k));
    std::string f = feed::encode_account_frame(k, a->raw.owner, a->raw.data.data(), a->raw.data.size(), a->lamports, slot, wv,
                                               "accounts", 0, bank);
    h.on_frame(reinterpret_cast<const uint8_t*>(f.data()), f.size(), 1);
  };
  auto slot_status = [&](uint64_t slot, uint32_t status, uint64_t bank) {
    std::string f = feed::encode_slot_frame(slot, status, &bank);
    h.on_frame(reinterpret_cast<const uint8_t*>(f.data()), f.size(), 1);
  };
  // The parser reads bank_id on account and slot updates.
  uint64_t b7 = 7;
  std::string f = feed::encode_account_frame(def->address, prog::RAYDIUM_V4, nullptr, 0, 1, 2000, 1, "accounts", 0, &b7);
  feed::UpdateView v;
  CHECK(feed::parse_update(reinterpret_cast<const uint8_t*>(f.data()), f.size(), v) && v.account.has_bank && v.account.bank_id == 7);
  // Slot 2000, bank 1: the pool and both vaults (plus mints) arrive and the pool is quotable.
  uint64_t bank1 = 1, bank2 = 2;
  for (const Pubkey& k : feed::pool_ordered_keys(store, {id})) push(k, 2000, 10, &bank1);
  CHECK(store.pool(id)->quotable(2));
  CHECK_EQ(h.banks().updates_with_bank, uint64_t(5));
  // A second candidate block for the same slot: everything touched in slot 2000 becomes suspect.
  slot_status(2000, 5, bank2);  // CreatedBank
  CHECK_EQ(h.banks().contested_slots, uint64_t(1));
  CHECK(!store.pool(id)->quotable(2));
  // Bank 2 wins: the accounts bank 1 wrote are queued for a re-read, and stay suspect until it lands.
  slot_status(2000, 1, bank2);  // Confirmed
  auto keys = h.take_resync();
  CHECK_EQ(keys.size(), size_t(5));
  CHECK(!store.pool(id)->quotable(2));
  // A trusted re-read (streams live) makes the pool quotable again.
  for (const Pubkey& k : keys) {
    const FixtureAccount* a = fixture(b58(k));
    AccountUpdate u{k, a->raw.owner, a->raw.data.data(), a->raw.data.size(), 2001, 0, false, false};
    store.apply(u);
  }
  CHECK(store.pool(id)->quotable(2));
  // An uncontested slot whose only bank is confirmed queues nothing.
  for (const Pubkey& k : feed::pool_ordered_keys(store, {id})) push(k, 2002, 11, &bank1);
  slot_status(2002, 1, bank1);
  CHECK(h.take_resync().empty());
  CHECK(store.pool(id)->quotable(2));
}
