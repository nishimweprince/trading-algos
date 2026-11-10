// live-plan: pick the pools to arm from the dry-run record and print what arming them costs. Read-only: it reads
// the decision log and pools.json, and writes the armed set only with --write. No RPC, no wallet, nothing sent.
//
//   live-plan CONFIG [--hours 24] [--max-pools 250] [--write /var/lib/jito-arb/armed.json]
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <sstream>

#include "app/live_plan.hpp"
#include "app/live_send.hpp"
#include "core/base58.hpp"
#include "core/config.hpp"
#include "core/crypto.hpp"
#include "core/sys.hpp"

using namespace arb;

int main(int argc, char** argv) {
  try {
    if (argc < 2) throw std::runtime_error("usage: live-plan CONFIG [--hours N] [--max-pools N] [--write PATH]");
    crypto_init();
    Config cfg = load_config(argv[1]);
    int hours = 24;
    size_t max_pools = 250;
    std::string write;
    for (int i = 2; i + 1 < argc; i += 2) {
      if (!std::strcmp(argv[i], "--hours")) hours = std::atoi(argv[i + 1]);
      else if (!std::strcmp(argv[i], "--max-pools")) max_pools = size_t(std::atoi(argv[i + 1]));
      else if (!std::strcmp(argv[i], "--write")) write = argv[i + 1];
      else throw std::runtime_error(std::string("unknown option ") + argv[i]);
    }
    std::ifstream pf(cfg.pools_path);
    if (!pf) throw std::runtime_error("cannot read " + cfg.pools_path);
    std::stringstream ps;
    ps << pf.rdbuf();
    app::PoolsFile pools;
    std::string err;
    if (!app::from_json(ps.str(), pools, &err)) throw std::runtime_error(err);

    // Only dry-run candidates and their outcomes are kept: skip records are most of the log.
    const int64_t since = wall_ms() - int64_t(hours) * 3'600'000;
    std::vector<obs::DecisionRecord> recs;
    size_t files = 0;
    for (auto& e : std::filesystem::directory_iterator(cfg.obs.decision_log_dir)) {
      const std::string name = e.path().filename().string();
      if (name.rfind("decisions-", 0) != 0 || e.path().extension() != ".bin") continue;
      obs::DecisionReader rd;
      if (!rd.open(e.path().string())) continue;
      ++files;
      obs::DecisionRecord r;
      while (rd.next(r)) {
        if (r.wall_ms < since) continue;
        const bool cand = r.kind == uint8_t(obs::RecordKind::Candidate) && r.action == uint8_t(obs::Action::DryRun);
        if (cand || r.kind == uint8_t(obs::RecordKind::Outcome)) recs.push_back(r);
      }
    }
    std::vector<Pubkey> bases;
    for (const std::string& m : cfg.engine.base_mints) bases.push_back(parse_pubkey(m));
    auto ranking = app::rank_pools(recs, int64_t(cfg.engine.min_net_lamports), 20);
    app::LivePlan plan = app::size_plan(pools, ranking, max_pools, bases);
    std::printf("window: last %d h, %zu decision files, %zu dry-run records\n", hours, files, recs.size());
    std::fputs(app::plan_report(plan, ranking.size(), max_pools).c_str(), stdout);
    for (size_t i = 0; i < ranking.size() && i < 10; ++i)
      std::printf("  %-44s passes %u  net %+.5f SOL\n", b58(ranking[i].pool).c_str(), ranking[i].passes, double(ranking[i].net_lamports) / 1e9);
    if (!write.empty()) {
      std::ofstream out(write);
      out << app::armed_to_json(plan.armed);
      if (!out) throw std::runtime_error("cannot write " + write);
      std::printf("wrote %zu pools to %s\n", plan.armed.size(), write.c_str());
    }
    return 0;
  } catch (const std::exception& e) {
    std::fprintf(stderr, "live-plan: %s\n", e.what());
    return 1;
  }
}
