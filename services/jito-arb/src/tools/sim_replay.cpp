// Revalidate recorded dry-run candidates against CURRENT chain state (not historical execution).
// sim-replay --config FILE --input DECISIONS.bin [--limit N] [--output DIR]
// Uses only getMultipleAccounts/getTokenAccounts/simulateTransaction. No signing or submission.
#include <chrono>
#include <cstdio>
#include <fstream>
#include <sstream>
#include <thread>
#include <filesystem>

#include "app/bundle_sim.hpp"
#include "app/pools_file.hpp"
#include "core/base58.hpp"
#include "core/crypto.hpp"
#include "core/rpc.hpp"
#include "core/sys.hpp"
#include "feed/resync.hpp"
#include "obs/log.hpp"

using namespace arb;
int main(int argc, char** argv) {
  try {
    std::string config, input, output;
    size_t limit = 3;
    for (int i = 1; i + 1 < argc; i += 2) {
      std::string a = argv[i];
      if (a == "--config") config = argv[i + 1];
      else if (a == "--input") input = argv[i + 1];
      else if (a == "--output") output = argv[i + 1];
      else if (a == "--limit") limit = std::stoull(argv[i + 1]);
      else throw std::runtime_error("unknown option");
    }
    if (config.empty() || input.empty() || limit == 0 || limit > 64)
      throw std::runtime_error("usage: sim-replay --config FILE --input DECISIONS.bin [--limit 1..64] [--output DIR]");
    crypto_init();
    obs::Logger::get().start("", obs::Level::info);
    auto cfg = load_config(config);
    cfg.sim.enabled = true;
    if (!output.empty()) cfg.obs.decision_log_dir = output;
    std::filesystem::create_directories(cfg.obs.decision_log_dir);
    std::ifstream f(cfg.pools_path);
    std::stringstream ss;
    ss << f.rdbuf();
    app::PoolsFile pf;
    std::string err;
    if (!app::from_json(ss.str(), pf, &err)) throw std::runtime_error(err);
    Rpc rpc(resolve_rpc_url(cfg), 15000);
    std::atomic<bool> stop{false};
    app::BundleSim sim(cfg, stop);
    sim.start();
    obs::DecisionReader reader;
    if (!reader.open(input)) throw std::runtime_error("cannot open decision log");
    obs::DecisionRecord cand;
    size_t count = 0;
    while (count < limit && reader.next(cand)) {
      if (cand.kind != uint8_t(obs::RecordKind::Candidate) || cand.action != uint8_t(obs::Action::DryRun) || cand.hops < 2 || cand.hops > 3) continue;
      StateStore store;
      app::SimJob j;
      j.cand = cand;
      j.c.id = cand.cycle_id;
      j.c.n = cand.hops;
      j.c.base = key_from(cand.base);
      j.o.x = cand.x;
      j.o.final_out = cand.final_out;
      j.o.flash_fee = cand.flash_fee;
      j.o.profit_base = cand.profit_base;
      bool found = true;
      for (int i = 0; i < cand.hops; ++i) {
        Pubkey address = key_from(cand.pools[i]);
        auto it = std::find_if(pf.pools.begin(), pf.pools.end(), [&](const auto& p) { return p.address == address; });
        if (it == pf.pools.end()) { found = false; break; }
        uint32_t id = store.add_pool(*it);
        bool d = (cand.a_to_b_mask >> i) & 1;
        j.c.legs[i] = engine::Leg{id, d, d ? it->mint_a : it->mint_b, d ? it->mint_b : it->mint_a};
        j.o.leg_out[i] = cand.leg_out[i];
      }
      if (!found) continue;
      feed::resync(rpc, store, store.subscribed_keys(), true);
      for (int i = 0; i < cand.hops; ++i) j.legs[i] = store.pool(j.c.legs[i].pool);
      if (sim.submit(std::move(j))) ++count;
    }
    auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(120);
    obs::DecisionLog log(cfg.obs.decision_log_dir, cfg.obs.decision_log_rotate_s);
    size_t outcomes = 0;
    while (outcomes < count && std::chrono::steady_clock::now() < deadline) {
      for (auto& r : sim.drain()) {
        ++outcomes;
        log.write(r);
        std::printf("current-state diagnostic signature=%s outcome=%u reason=%u sim_out=%llu quoted=%llu units=%u net=%lld\n",
          b58(r.signature, 64).c_str(), unsigned(r.outcome), unsigned(r.reason), (unsigned long long)r.final_out,
          (unsigned long long)r.min_out, r.cu_limit, (long long)r.net_lamports);
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    sim.shutdown();
    for (auto& r : sim.drain()) { log.write(r); ++outcomes; }
    const auto& c = sim.counters();
    std::printf("submitted=%llu terminal=%llu ok=%llu failed=%llu no_holder=%llu rpc=%llu funding_changed=%llu inventory=%llu\n",
      (unsigned long long)c.submitted.load(), (unsigned long long)c.terminal.load(), (unsigned long long)c.ok.load(),
      (unsigned long long)c.failed.load(), (unsigned long long)c.no_holder.load(), (unsigned long long)c.rpc_error.load(),
      (unsigned long long)c.funding_changed.load(), (unsigned long long)c.intermediate_balance.load());
    obs::Logger::get().stop();
    return count && outcomes == count && c.ok ? 0 : 1;
  } catch (const std::exception& e) {
    std::fprintf(stderr, "sim-replay: %s\n", e.what());
    obs::Logger::get().stop();
    return 1;
  }
}
