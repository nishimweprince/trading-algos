// Pre-live hardening: feed health gate and alerts, PDA memo, tip policy, arming (token accounts, lookup tables).
#include <cstring>
#include <set>
#include <string>

#include "app/arming.hpp"
#include "app/bundle_builder.hpp"

#include "core/crypto.hpp"
#include "core/base58.hpp"
#include "core/programs.hpp"
#include "engine/costs.hpp"
#include "feed/health.hpp"
#include "obs/alert.hpp"
#include "state/state_store.hpp"
#include "submit/jito.hpp"
#include "tx/builders.hpp"
#include "test.hpp"

using namespace arb;

namespace {

feed::FeedHealthSample sample(int64_t ms, int up, uint64_t rc, int64_t age) {
  feed::FeedHealthSample s;
  s.now_ms = ms;
  s.shards_up = up;
  s.shards_total = 9;
  s.reconnects_total = rc;
  s.update_age_ms = age;
  return s;
}

}  // namespace

TEST(feed_health_healthy_stream_passes) {
  feed::FeedHealth h;
  CHECK(h.update(sample(1000, 9, 0, 5)) == feed::FeedEvent::none);
  CHECK(h.ok());
  CHECK(h.reason().empty());
}

TEST(feed_health_gate_closes_at_once_and_alerts_after_grace) {
  feed::FeedHealth h;
  h.update(sample(0, 9, 0, 5));
  CHECK(h.update(sample(1000, 8, 1, 5)) == feed::FeedEvent::none);
  CHECK(!h.ok());  // no grace for sending
  CHECK(h.reason().find("streams down") != std::string::npos);
  CHECK(h.update(sample(30'000, 8, 1, 5)) == feed::FeedEvent::none);
  CHECK(h.update(sample(61'000, 8, 1, 5)) == feed::FeedEvent::down);
  CHECK(h.update(sample(62'000, 8, 1, 5)) == feed::FeedEvent::none);  // one alert, not one per second
  CHECK(h.update(sample(61'000 + 1'800'000, 8, 1, 5)) == feed::FeedEvent::still_down);
}

// The Nov 11-16 2026 pattern: every shard subscribes, then the read fails at once, every 10 s. `up` is often
// true at the sample instant and the update age alone may look fine (resync writes), so the reconnect rate
// must close the gate on its own.
TEST(feed_health_flapping_streams_close_the_gate) {
  feed::FeedHealth h;
  uint64_t rc = 0;
  feed::FeedEvent last = feed::FeedEvent::none;
  bool any_ok_after_first_minute = false;
  for (int64_t t = 0; t <= 120'000; t += 1000) {
    if (t % 10'000 == 0) rc += 9;  // all shards reconnect every 10 s
    last = h.update(sample(t, 9, rc, 5));
    if (t >= 70'000 && h.ok()) any_ok_after_first_minute = true;
    if (last == feed::FeedEvent::down) break;
  }
  CHECK(!h.ok());
  CHECK(h.reason().find("flapping") != std::string::npos);
  CHECK(last == feed::FeedEvent::down);
  CHECK(!any_ok_after_first_minute);
}

TEST(feed_health_stale_updates_close_the_gate) {
  feed::FeedHealth h;
  h.update(sample(0, 9, 0, 900));
  CHECK(!h.ok());
  CHECK(h.reason().find("old") != std::string::npos);
  h = feed::FeedHealth();
  h.update(sample(0, 9, 0, -1));
  CHECK(!h.ok());
}

TEST(feed_health_recovers_after_a_healthy_minute) {
  feed::FeedHealth h;
  h.update(sample(0, 0, 0, -1));
  CHECK(h.update(sample(60'000, 0, 0, -1)) == feed::FeedEvent::down);
  CHECK(h.update(sample(61'000, 9, 0, 5)) == feed::FeedEvent::none);
  CHECK(h.ok());
  CHECK(h.update(sample(121'000, 9, 0, 5)) == feed::FeedEvent::recovered);
}

TEST(telegram_body_escapes) {
  std::string b = obs::telegram_body("-100", "a \"b\"\nc\\d");
  CHECK_EQ(b, std::string("{\"chat_id\":\"-100\",\"text\":\"a \\\"b\\\"\\nc\\\\d\",\"disable_web_page_preview\":true}"));
}

TEST(pda_memo_matches_uncached_derivation) {
  crypto_init();
  for (int i = 0; i < 64; ++i) {
    Pubkey owner{}, mint{};
    owner[0] = uint8_t(i);
    mint[5] = uint8_t(i * 7);
    Pubkey direct;
    CHECK(find_program_address({owner, prog::TOKEN, mint}, prog::ATA, direct));
    CHECK(ata(owner, mint, prog::TOKEN) == direct);
    CHECK(ata(owner, mint, prog::TOKEN) == direct);          // memo hit
    CHECK(ata(owner, mint, prog::TOKEN_2022) != direct);     // a different seed list is a different key
  }
  // On chain, seeds are hashed back to back, so ["ab","c"] and ["a","bc"] are the same address; the memo keys
  // them separately (lengths are in the key) and must still return the derived value for each.
  Pubkey a = pda({std::string_view("ab"), std::string_view("c")}, prog::WHIRLPOOL);
  Pubkey b = pda({std::string_view("a"), std::string_view("bc")}, prog::WHIRLPOOL);
  Pubkey direct;
  CHECK(find_program_address({std::string_view("abc")}, prog::WHIRLPOOL, direct));
  CHECK(a == direct);
  CHECK(b == direct);
}


TEST(tip_state_round_trips_and_clamps) {
  engine::TipPolicy t;
  for (int i = 0; i < 60; ++i) t.record(7, i % 10 == 0);  // 6/60 landed: one window below target raises alpha
  t.record(9, true);
  CHECK_EQ(t.attempts(), uint64_t(61));
  engine::TipPolicy u;
  CHECK(u.load(t.save()));
  CHECK_EQ(u.alpha_ppm(7), t.alpha_ppm(7));
  CHECK_EQ(u.p_land_ppm(7), t.p_land_ppm(7));
  CHECK_EQ(u.p_land_ppm(9), t.p_land_ppm(9));
  CHECK_EQ(u.attempts(), t.attempts());
  CHECK(u.load("# comment\n3 5000000 10 2 0 0\n"));
  CHECK_EQ(u.alpha_ppm(3), uint32_t(950'000));  // re-clamped to alpha_max
  CHECK(!u.load("3 500000 1 2 0 0\n"));         // landed > attempts
  CHECK_EQ(u.alpha_ppm(3), uint32_t(950'000));  // a bad file leaves the state alone
}

TEST(tip_floor_percentiles_parse) {
  std::string body = R"([{"time":"2026-11-17T00:00:00Z","landed_tips_25th_percentile":0.000005,"landed_tips_50th_percentile":0.00001,)"
                     R"("landed_tips_75th_percentile":0.00005,"landed_tips_95th_percentile":0.001,"landed_tips_99th_percentile":0.01}])";
  CHECK_EQ(submit::parse_tip_floor(body, 25), uint64_t(5000));
  CHECK_EQ(submit::parse_tip_floor(body, 50), uint64_t(10000));
  CHECK_EQ(submit::parse_tip_floor_p25(body), uint64_t(5000));
  CHECK_EQ(submit::parse_tip_floor(body, 60), uint64_t(0));
}


namespace {
Pubkey key_n(uint8_t n) {
  Pubkey k{};
  k[0] = n;
  k[31] = 0x5a;
  return k;
}
}  // namespace

TEST(arming_needs_dedupe_and_leave_out_wallet_and_placeholder) {
  crypto_init();
  StateStore store;
  const Pubkey wsol = parse_pubkey("So11111111111111111111111111111111111111112");
  for (uint8_t i = 0; i < 3; ++i) {  // three SOL pools, two sharing a mint
    PoolDef d;
    d.kind = DexKind::RaydiumV4;
    d.address = key_n(uint8_t(10 + i));
    d.mint_a = wsol;
    d.mint_b = key_n(i == 2 ? 20 : uint8_t(20 + i));
    d.vault_a = key_n(uint8_t(30 + i));
    d.vault_b = key_n(uint8_t(40 + i));
    store.add_pool(d);
  }
  const Pubkey w = key_n(99);
  auto atas = app::needed_atas(store, w);
  CHECK_EQ(atas.size(), size_t(3));  // wSOL + two distinct other mints
  for (const auto& a : atas) CHECK(a.ata == ata(w, a.mint, prog::TOKEN));

  auto keys = app::needed_table_keys(store, w, {}, std::nullopt);
  std::set<Pubkey> uniq(keys.begin(), keys.end());
  CHECK_EQ(uniq.size(), keys.size());
  CHECK(!uniq.count(w));
  CHECK(!uniq.count(app::placeholder_executor()));
  CHECK(uniq.count(key_n(10)) && uniq.count(key_n(42)));
  for (const auto& a : atas) CHECK(uniq.count(a.ata));
  auto with_exec = app::needed_table_keys(store, w, {}, key_n(77));
  CHECK_EQ(with_exec.size(), keys.size() + 1);

  auto txs = app::ata_txs(atas, w, 2);
  CHECK_EQ(txs.size(), size_t(2));
  app::add_compute_budget(txs, 200'000, 20'000);
  CHECK_EQ(int(txs[0].n), 4);  // limit, price, 2 creates
  CHECK(txs[0].ixs[0].program == prog::COMPUTE_BUDGET && txs[0].ixs[2].program == prog::ATA);
  CHECK_EQ(int(txs[0].ixs[2].data[0]), 1);  // CreateIdempotent
  for (const auto& t : txs) CHECK(app::setup_programs_allowed(t));
  tx::IxList bad = txs[0];
  tx::system_transfer(bad, w, key_n(5), 1);
  CHECK(!app::setup_programs_allowed(bad));
}

TEST(arming_tables_json_round_trip) {
  std::vector<Pubkey> t{key_n(1), key_n(2)}, back;
  CHECK(app::tables_from_json(app::tables_to_json(t), back));
  CHECK(back == t);
  std::string err;
  CHECK(!app::tables_from_json("{\"tables\":[\"nope\"]}", back, &err));
  CHECK(back == t);  // unchanged on error
  CHECK(!app::tables_from_json("{}", back));
}

// The plan the tool sends: missing keys only, at most 256 per table, 20 per extend, one create per new table.
TEST(arming_extend_plan_fills_existing_then_creates) {
  tx::AltManager alts;
  const Pubkey w = key_n(99);
  tx::AltAccount have;
  have.has_authority = true;
  have.authority = w;
  for (int i = 0; i < 250; ++i) have.addresses.push_back(key_n(uint8_t(i)));
  alts.upsert(key_n(250), have);
  std::vector<Pubkey> keys;
  for (int i = 0; i < 300; ++i) {
    Pubkey k{};
    k[0] = uint8_t(i);
    k[1] = uint8_t(i >> 8);
    k[31] = 0x5a;
    keys.push_back(k);
  }
  auto missing = alts.missing(keys);
  std::vector<Pubkey> created;
  auto plan = alts.plan_extend(missing, w, w, 1000, &created);
  CHECK_EQ(created.size(), size_t(1));
  size_t added = 0, creates = 0;
  for (const auto& l : plan)
    for (uint8_t j = 0; j < l.n; ++j)
      if (l.ixs[j].program == prog::ALT) {
        uint32_t op;
        std::memcpy(&op, l.ixs[j].data, 4);
        if (op == 0) ++creates;
        else added += size_t(l.ixs[j].data_len - 12) / 32;
      }
  CHECK_EQ(creates, size_t(1));
  CHECK_EQ(added, missing.size());
}

#include "app/send_audit.hpp"

TEST(send_audit_classifies_simulation_errors) {
  CHECK(app::classify_sim_error("") == app::AuditVerdict::ok);
  CHECK(app::classify_sim_error("{\"InstructionError\":[3,{\"Custom\":6002}]}") == app::AuditVerdict::min_out);
  CHECK(app::classify_sim_error("{\"InstructionError\":[2,{\"Custom\":6018}]}") == app::AuditVerdict::other);
  CHECK(app::classify_sim_error("\"AccountNotFound\"") == app::AuditVerdict::other);
}

#include <thread>
#include "submit/router.hpp"

namespace {
submit::LeaderInfo leader(submit::LeaderKind k, const char* region) {
  submit::LeaderInfo l;
  l.identity = "X";
  l.kind = k;
  l.region = region;
  return l;
}
}  // namespace

TEST(nearest_region_by_distance) {
  std::vector<std::string> all{"frankfurt", "amsterdam", "london", "dublin", "ny", "slc", "tokyo", "singapore"};
  CHECK_EQ(submit::nearest_region(50.1, 8.7, all), std::string("frankfurt"));   // Frankfurt
  CHECK_EQ(submit::nearest_region(52.4, 4.9, all), std::string("amsterdam"));   // Amsterdam
  CHECK_EQ(submit::nearest_region(51.5, -0.1, all), std::string("london"));
  CHECK_EQ(submit::nearest_region(41.9, -87.6, all), std::string("ny"));        // Chicago -> ny
  CHECK_EQ(submit::nearest_region(56.0, 23.3, all), std::string("frankfurt"));  // Siauliai, Lithuania
  CHECK_EQ(submit::nearest_region(1.3, 103.8, all), std::string("singapore"));
  CHECK_EQ(submit::nearest_region(41.9, -87.6, {"frankfurt"}), std::string("frankfurt"));
}

TEST(route_plan_rules) {
  submit::RouterConfig c;
  c.regions = {"frankfurt", "amsterdam"};
  c.always = {"frankfurt"};
  using K = submit::LeaderKind;
  auto p = submit::plan_route(c, leader(K::jito, "london"), leader(K::jito, "ny"));
  CHECK(!p.skip);
  CHECK((p.block_engine == std::vector<std::string>{"frankfurt", "london", "ny"}));
  CHECK(p.bam.empty());
  p = submit::plan_route(c, leader(K::not_jito, "ny"), leader(K::not_jito, "tokyo"));
  CHECK(p.skip);
  p = submit::plan_route(c, leader(K::not_jito, "ny"), leader(K::jito, "tokyo"));
  CHECK(!p.skip && (p.block_engine == std::vector<std::string>{"frankfurt", "tokyo"}));
  p = submit::plan_route(c, leader(K::bam, "amsterdam"), leader(K::bam, "amsterdam"));
  CHECK(!p.skip && p.bam.empty());  // no UUID: block engine only
  c.skip_bam = true;
  CHECK(submit::plan_route(c, leader(K::bam, "amsterdam"), leader(K::bam, "amsterdam")).skip);
  c.bam_uuid = "u";
  p = submit::plan_route(c, leader(K::bam, "amsterdam"), leader(K::jito, "london"));
  CHECK(!p.skip && (p.bam == std::vector<std::string>{"amsterdam"}));
  // Unknown leaders (map not loaded yet): the configured regions, never a skip.
  p = submit::plan_route(c, submit::LeaderInfo{}, submit::LeaderInfo{});
  CHECK(!p.skip && (p.block_engine == std::vector<std::string>{"frankfurt", "amsterdam"}));
}

namespace {
class RecordingHttp final : public submit::HttpPost {
 public:
  RecordingHttp(std::string url, std::string headers, std::mutex* mu, std::vector<std::string>* log) : url_(std::move(url)), headers_(std::move(headers)), mu_(mu), log_(log) {}
  int post(std::string_view path, std::string_view body, std::string& out) override {
    std::lock_guard<std::mutex> g(*mu_);
    log_->push_back(url_ + "|" + headers_ + "|" + std::string(path) + "|" + std::string(body));
    out = "{\"jsonrpc\":\"2.0\",\"result\":\"bid1\",\"id\":1}";
    return 200;
  }
 private:
  std::string url_, headers_;
  std::mutex* mu_;
  std::vector<std::string>* log_;
};
}  // namespace

TEST(router_sends_async_per_region_and_drops_passed_slots) {
  std::mutex mu;
  std::vector<std::string> posts;
  std::vector<submit::SendResult> done;
  submit::RouterConfig c;
  c.regions = {"frankfurt", "amsterdam"};
  c.always = {"frankfurt"};
  c.keepalive_s = 0;  // count bundle posts only
  c.http = [&](const std::string& url, const std::string& h) { return std::unique_ptr<submit::HttpPost>(new RecordingHttp(url, h, &mu, &posts)); };
  std::atomic<uint64_t> slot{100};
  submit::Router r(c, nullptr, &slot, [&](const submit::SendResult& s, const Signature&, uint16_t, uint64_t, const submit::LeaderInfo&, int64_t) {
    std::lock_guard<std::mutex> g(mu);
    done.push_back(s);
  });
  Signature sig{};
  auto plan = r.route("dHg=", sig, 520, 100, 0);  // no leader map: the configured regions
  CHECK(!plan.skip && plan.block_engine.size() == 2);
  for (int i = 0; i < 200; ++i) {
    {
      std::lock_guard<std::mutex> g(mu);
      if (done.size() == 2) break;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(5));
  }
  {
    std::lock_guard<std::mutex> g(mu);
    CHECK_EQ(done.size(), size_t(2));
    for (auto& d : done) CHECK(d.bundle_id == "bid1" && d.http_status == 200 && !d.dropped_stale);
    CHECK_EQ(posts.size(), size_t(2));
  }
  CHECK_EQ(r.accepted(), uint64_t(2));
  // A back-run: the preconfirmed trigger goes first in the same bundle, ours last (it carries the tip).
  {
    std::vector<std::string> prefix{"dHJpZw=="};
    r.route("dHg=", sig, 520, 100, 0, &prefix);
    for (int i = 0; i < 200 && r.accepted() < 4; ++i) std::this_thread::sleep_for(std::chrono::milliseconds(5));
    std::lock_guard<std::mutex> g(mu);
    CHECK_EQ(posts.size(), size_t(4));
    CHECK(posts[2].find("[[\"dHJpZw==\",\"dHg=\"]") != std::string::npos);
  }
  slot = 105;  // the slot passed before a sender picked it up: dropped, never sent late
  r.route("dHg=", sig, 520, 100, 0);
  for (int i = 0; i < 200 && r.dropped() < 2; ++i) std::this_thread::sleep_for(std::chrono::milliseconds(5));
  CHECK_EQ(r.dropped(), uint64_t(2));
  std::lock_guard<std::mutex> g(mu);
  CHECK_EQ(posts.size(), size_t(4));
}

TEST(adaptive_cu_limit_learns_from_measured_use) {
  engine::TipPolicy t;
  CHECK_EQ(t.cu_limit(520, 391'000), uint32_t(391'000));  // no samples: the estimate
  t.observe_cu(520, 120'000);
  t.observe_cu(520, 122'000);
  CHECK_EQ(t.cu_limit(520, 391'000), uint32_t(391'000));  // fewer than 3
  t.observe_cu(520, 126'000);
  CHECK_EQ(t.cu_limit(520, 391'000), uint32_t(126'000 + 31'500 + 10'000));  // max x 1.25 + 10k
  CHECK_EQ(t.cu_limit(520, 150'000), uint32_t(150'000));  // never above the estimate
  CHECK_EQ(t.cu_limit(776, 391'000), uint32_t(391'000));  // other classes untouched
  for (int i = 0; i < 20; ++i) t.observe_cu(9, 10'000);
  CHECK_EQ(t.cu_limit(9, 300'000), uint32_t(50'000));     // never below 50k
  CHECK_EQ(t.cu_samples(9), size_t(16));                  // last 16 kept
  // price() uses it: same quote, the learned class asks for less compute.
  engine::BasePrice bp;
  engine::CostConfig cc;
  cc.min_ev_lamports = 0;
  auto a = engine::price(1'000'000'000, 5'000'000, 0, 280'000, 520, bp, t, cc);
  auto b = engine::price(1'000'000'000, 5'000'000, 0, 280'000, 776, bp, t, cc);
  CHECK(a.ok && b.ok);
  CHECK(a.cu_limit < b.cu_limit && a.cu_limit == uint32_t(167'500));
}

TEST(router_warms_every_region_and_pings_idle_connections) {
  std::mutex mu;
  std::vector<std::string> posts;
  submit::RouterConfig c;
  c.keepalive_s = 1;
  c.http = [&](const std::string& url, const std::string& h) { return std::unique_ptr<submit::HttpPost>(new RecordingHttp(url, h, &mu, &posts)); };
  submit::Router r(c, nullptr, nullptr, [](const submit::SendResult&, const Signature&, uint16_t, uint64_t, const submit::LeaderInfo&, int64_t) {});
  r.warm({"frankfurt", "tokyo"});
  auto count = [&](const std::string& region) {
    std::lock_guard<std::mutex> g(mu);
    return size_t(std::count_if(posts.begin(), posts.end(), [&](const std::string& p) {
      return p.find(region) != std::string::npos && p.find("getTipAccounts") != std::string::npos;
    }));
  };
  for (int i = 0; i < 200 && (count("frankfurt") < 1 || count("tokyo") < 1); ++i) std::this_thread::sleep_for(std::chrono::milliseconds(5));
  CHECK(count("frankfurt") >= 1 && count("tokyo") >= 1);  // opened at once, before any bundle
  std::this_thread::sleep_for(std::chrono::milliseconds(2300));
  CHECK(count("frankfurt") >= 2 && count("tokyo") >= 2);  // pinged again while idle
  CHECK(r.keepalive_rtt().count("tokyo") == 1 && r.keepalive_failures() == 0);
}
