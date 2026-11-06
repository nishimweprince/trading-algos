#include "app/bundle_sim.hpp"
#include "core/base58.hpp"
#include "core/programs.hpp"
#include "test.hpp"
using namespace arb;

TEST(bundle_sim_outstanding_capacity_and_shutdown_reconciliation) {
  Config cfg;
  cfg.sim.holder_seed_path = std::string(ARB_SOURCE_DIR) + "/harness/holders_seed.json";
  cfg.sim.max_per_s = 1000;
  std::atomic<bool> stop{false};
  app::BundleSim sim(cfg, stop);  // no worker: deterministic capacity and shutdown exercise
  for (int i = 0; i < 100; ++i) sim.submit(app::SimJob{});
  CHECK_EQ(sim.counters().submitted.load(), uint64_t(100));
  CHECK_EQ(sim.counters().queued.load(), uint64_t(64));
  CHECK_EQ(sim.counters().dropped.load(), uint64_t(36));
  sim.shutdown();
  auto done = sim.drain();
  CHECK_EQ(done.size(), size_t(64));
  CHECK_EQ(sim.counters().terminal.load(), uint64_t(64));
  CHECK_EQ(sim.counters().queued.load(), uint64_t(0));
  CHECK_EQ(sim.counters().in_flight.load(), uint64_t(0));
  for (auto& r : done) CHECK(r.reason == uint8_t(obs::SimFail::shutdown));
  CHECK_EQ(sim.counters().submitted.load(), sim.counters().terminal.load() + sim.counters().dropped.load());
}

TEST(bundle_sim_top_level_cu_excludes_nested_same_program) {
  auto ray = std::make_shared<PoolState>(), wp = std::make_shared<PoolState>();
  ray->kind = DexKind::RaydiumV4;
  wp->kind = DexKind::Whirlpool;
  std::shared_ptr<const PoolState> legs[2] = {ray, wp};
  std::string r = b58(prog::RAYDIUM_V4), w = b58(prog::WHIRLPOOL), t = b58(prog::TOKEN);
  auto units = app::sim_leg_units({"Program " + r + " invoke [1]", "Program " + t + " invoke [2]",
    "Program " + t + " consumed 123 of 100000 compute units", "Program " + t + " success",
    "Program " + r + " consumed 30000 of 100000 compute units", "Program " + r + " success",
    "Program " + w + " invoke [1]", "Program " + w + " consumed 50000 of 100000 compute units",
    "Program " + w + " success"}, legs);
  CHECK_EQ(units[0], uint64_t(30000));
  CHECK_EQ(units[1], uint64_t(50000));
}
