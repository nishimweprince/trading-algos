#include "app/bundle_sim.hpp"
#include "core/base58.hpp"
#include "core/programs.hpp"
#include "test.hpp"
#include "tx/dex_ix.hpp"
#include "core/crypto.hpp"
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

TEST(pumpswap_sim_setup_matches_pinned_idl) {
  crypto_init();
  Pubkey user = parse_pubkey("2yik7ZsLfFKAmmr9cCHhL1dTabiECnFKDeSVyTbxeETU");
  tx::IxList ixs;
  tx::dexix::pump_init_user_volume(ixs, user);
  auto& ix = ixs.ixs[0];
  const uint8_t disc[8] = {94,6,202,115,255,96,232,183};
  CHECK_EQ(ix.n_accounts, uint8_t(6));
  CHECK_EQ(ix.data_len, uint16_t(8));
  CHECK(std::memcmp(ix.data, disc, 8) == 0);
  CHECK(ix.program == prog::PUMPSWAP);
  CHECK(ix.accounts[0].key == user && ix.accounts[0].signer && ix.accounts[0].writable);
  CHECK(ix.accounts[1].key == user && !ix.accounts[1].signer);
  CHECK(ix.accounts[2].key == tx::dexix::pump_user_volume_accumulator(user) && ix.accounts[2].writable);
  CHECK(ix.accounts[3].key == prog::SYSTEM);
  CHECK(ix.accounts[4].key == tx::dexix::pump_event_authority());
  CHECK(ix.accounts[5].key == prog::PUMPSWAP);
}

TEST(pumpswap_sim_rent_topup_is_holder_funded_system_transfer) {
  crypto_init();
  Pubkey user = parse_pubkey("2yik7ZsLfFKAmmr9cCHhL1dTabiECnFKDeSVyTbxeETU");
  tx::IxList ixs; tx::dexix::pump_fund_user_volume(ixs,user,166155);
  auto& ix=ixs.ixs[0];
  CHECK(ix.program==prog::SYSTEM && ix.n_accounts==2 && ix.data_len==12);
  CHECK(ix.accounts[0].key==user && ix.accounts[0].signer && ix.accounts[0].writable);
  CHECK(ix.accounts[1].key==tx::dexix::pump_user_volume_accumulator(user) && ix.accounts[1].writable);
  uint32_t tag=0; uint64_t amount=0;std::memcpy(&tag,ix.data,4);std::memcpy(&amount,ix.data+4,8);
  CHECK_EQ(tag,uint32_t(2));CHECK_EQ(amount,uint64_t(166155));
}

TEST(bundle_sim_stale_jobs_are_dropped_and_late_verdicts_ignored) {
  CHECK(!app::sim_job_stale(1'000, 1'900, 1'000));
  CHECK(app::sim_job_stale(1'000, 2'001, 1'000));
  CHECK(!app::sim_job_stale(1'000, 99'000, 0));  // 0 disables the check
  CHECK(!app::sim_job_stale(0, 99'000, 1'000));  // never submitted through submit()

  obs::DecisionRecord r{};
  r.state_slot[0] = 100;
  r.slot = 104;
  CHECK(app::sim_verdict_usable(r, 4));
  r.slot = 105;
  CHECK(!app::sim_verdict_usable(r, 4));  // the 13:30 burst ran 58-155 slots late
  CHECK(app::sim_verdict_usable(r, -1));  // negative disables the gate
  r.slot = 0;
  CHECK(!app::sim_verdict_usable(r, 4));  // not simulated: no verdict

  Config cfg;
  cfg.sim.holder_seed_path = std::string(ARB_SOURCE_DIR) + "/harness/holders_seed.json";
  cfg.sim.max_per_s = 1000;
  std::atomic<bool> stop{false};
  app::BundleSim sim(cfg, stop);
  CHECK(sim.submit(app::SimJob{}));
  CHECK_EQ(sim.counters().stale.load(), uint64_t(0));
  sim.shutdown();
  for (auto& d : sim.drain()) CHECK(d.reason == uint8_t(obs::SimFail::shutdown));
}
