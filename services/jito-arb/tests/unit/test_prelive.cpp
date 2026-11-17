// Pre-live hardening: feed health gate and alerts, PDA memo, tip policy.
#include <string>

#include "core/crypto.hpp"
#include "core/programs.hpp"
#include "feed/health.hpp"
#include "obs/alert.hpp"
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

#include "engine/costs.hpp"
#include "submit/jito.hpp"

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
