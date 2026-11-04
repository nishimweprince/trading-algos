// Phase 2: replay of the committed synthetic capture (tests/fixtures/frames.bin, built by fixture-frames
// from real mainnet account bytes plus injected gaps on synthetic twin pools) through the same
// Runtime as the live feed, into a decision log, then the per-day report.
#include <unistd.h>

#include <cstring>

#include "app/runtime.hpp"
#include "core/programs.hpp"
#include "fixture.hpp"
#include "obs/replay_report.hpp"
#include "test.hpp"

using namespace arb;
using namespace arbtest;

TEST(replay_frames_into_decision_log_and_report) {
  app::PoolsFile pf;
  std::string err;
  CHECK(app::from_json(read_file(fixture_path("replay_pools.json")), pf, &err));
  Config cfg;  // defaults: dry_run, flash funding, both DEXes enabled
  CHECK(cfg.mode.dry_run);
  app::Runtime rt(cfg, pf);
  char path[] = "/tmp/arb_decisions_XXXXXX";
  int fd = mkstemp(path);
  close(fd);
  obs::DecisionLog log("", 0);
  CHECK(log.open_path(path));
  rt.set_decision_log(&log);
  CHECK(rt.replay_file(fixture_path("frames.bin")));
  log.close();
  const auto& st = rt.stats();
  CHECK_EQ(st.bad_frames, uint64_t(0));
  std::printf("    replay: frames=%llu updates=%llu records=%llu dispatched=%llu cycles=%zu\n", (unsigned long long)st.frames,
              (unsigned long long)st.updates, (unsigned long long)st.records, (unsigned long long)st.sent, rt.cycles().size());
  CHECK(st.sent >= 40);  // 50 injected gaps; each should be found once
  auto recs = obs::read_records({path});
  CHECK_EQ(recs.size(), size_t(st.records));
  size_t dry = 0;
  for (auto& r : recs) {
    if (r.action != uint8_t(obs::Action::DryRun)) continue;
    ++dry;
    CHECK(r.min_out <= r.final_out);                     // the guard never demands more than the quote
    if (std::memcmp(r.base, mint::WSOL.data(), 32) == 0)  // wSOL: one base unit is one lamport
      CHECK(r.min_out >= r.x + r.flash_fee + r.tip_lamports + cfg.engine.min_net_lamports);
    CHECK(r.net_lamports >= int64_t(cfg.engine.min_net_lamports));
    CHECK(r.hops == 2 && r.cycle_id < rt.cycles().size());
  }
  CHECK_EQ(dry, size_t(st.sent));
  auto rep = obs::aggregate(recs, int64_t(cfg.engine.min_ev_lamports), cfg.engine.min_net_lamports);
  CHECK_EQ(rep.days.size(), size_t(2));
  uint64_t detections = 0;
  for (auto& [day, d] : rep.days) detections += d.detections;
  CHECK_EQ(detections, uint64_t(st.sent));
  std::string text = obs::to_text(rep);
  CHECK(text.find("2026-11-01") != std::string::npos && text.find("2026-11-02") != std::string::npos);
  std::string json = obs::to_json(rep);
  CHECK(json.find("\"gate_passes\"") != std::string::npos);
  std::remove(path);
}
