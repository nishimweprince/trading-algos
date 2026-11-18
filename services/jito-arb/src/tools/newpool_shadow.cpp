// newpool-shadow: do price gaps on NEW pools last long enough for a ~90 ms bot? Read-only measurement.
//
// Discovery writes every safe candidate pool (discovery.candidates_path). Each time that file changes, pools not
// seen before are new. For `--track-min` minutes after a new pool appears, this polls the new pool and every
// candidate pool sharing its token (plus the deepest SOL/USDC pools, for 3-hop routes and pricing) about once a
// second by RPC, and prices their cycles with the bot's own engine. Candidates the engine would dispatch go to a
// decision log; nothing is built, signed or sent. Own-capital pricing with a notional balance, so no flash-loan
// state is needed (the 0.1 bp flash fee is negligible here).
//
//   newpool-shadow CONFIG [--hours 24] [--track-min 60] [--poll-ms 1000] [--out DIR]
//
// The first candidates file only seeds the "seen" set (DIR/seen.txt): pools are new only if they appear later.
#include <sys/stat.h>

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <map>
#include <set>
#include <sstream>
#include <thread>

#include "app/pools_file.hpp"
#include "app/runtime.hpp"
#include "core/base58.hpp"
#include "core/config.hpp"
#include "core/crypto.hpp"
#include "core/programs.hpp"
#include "core/rpc.hpp"
#include "core/sys.hpp"
#include "feed/resync.hpp"
#include "obs/decision_log.hpp"
#include "obs/log.hpp"

using namespace arb;

namespace {

std::string read_file(const std::string& path) {
  std::ifstream f(path);
  std::stringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

int64_t mtime(const std::string& path) {
  struct stat st {};
  return ::stat(path.c_str(), &st) == 0 ? int64_t(st.st_mtime) : 0;
}

}  // namespace

int main(int argc, char** argv) {
  try {
    if (argc < 2) throw std::runtime_error("usage: newpool-shadow CONFIG [--hours 24] [--track-min 60] [--poll-ms 1000] [--out DIR]");
    double hours = 24;
    int track_min = 60, poll_ms = 1000;
    std::string out = "/var/lib/jito-arb/newpools";
    for (int i = 2; i + 1 < argc; i += 2) {
      std::string a = argv[i];
      if (a == "--hours") hours = std::stod(argv[i + 1]);
      else if (a == "--track-min") track_min = std::stoi(argv[i + 1]);
      else if (a == "--poll-ms") poll_ms = std::stoi(argv[i + 1]);
      else if (a == "--out") out = argv[i + 1];
      else throw std::runtime_error("unknown option " + a);
    }
    crypto_init();
    Config cfg = load_config(argv[1]);
    if (cfg.discovery.candidates_path.empty()) throw std::runtime_error("discovery.candidates_path is not set");
    // Shadow pricing: own capital with a notional balance, every candidate logged, skip records sampled lightly.
    cfg.mode.funding_mode = "own";
    cfg.obs.skip_log_max_per_s = 2;
    cfg.obs.decision_log_dir = out + "/decisions";
    std::filesystem::create_directories(cfg.obs.decision_log_dir);
    obs::Logger::get().start("", obs::Level::warn);
    Rpc rpc(resolve_rpc_url(cfg), 15'000);
    std::vector<Pubkey> bases;
    for (const std::string& m : cfg.engine.base_mints) bases.push_back(parse_pubkey(m));
    auto is_base = [&](const Pubkey& m) { return std::find(bases.begin(), bases.end(), m) != bases.end(); };

    // Seen set, persisted so a restart does not call every pool new.
    const std::string seen_path = out + "/seen.txt";
    std::set<std::string> seen;
    {
      std::ifstream f(seen_path);
      for (std::string l; std::getline(f, l);)
        if (!l.empty()) seen.insert(l);
    }
    bool seeded = !seen.empty();
    std::ofstream newlog(out + "/new_pools.tsv", std::ios::app);  // wall_ms, pool, dex, token, partners

    app::PoolsFile cands;
    int64_t cands_mtime = 0;
    std::map<Pubkey, int64_t> active;  // new pool -> first seen (wall ms)
    std::unique_ptr<app::Runtime> rt;
    std::unique_ptr<obs::DecisionLog> dlog;
    std::vector<Pubkey> keys;
    std::set<Pubkey> universe;
    uint64_t polls = 0, rebuilds = 0, new_total = 0;
    const auto t0 = std::chrono::steady_clock::now();
    auto last_status = t0;

    while (std::chrono::steady_clock::now() - t0 < std::chrono::duration<double, std::ratio<3600>>(hours)) {
      const int64_t now = wall_ms();
      // 1. New candidates file: diff against the seen set.
      if (int64_t m = mtime(cfg.discovery.candidates_path); m && m != cands_mtime) {
        std::string err;
        app::PoolsFile f;
        if (app::from_json(read_file(cfg.discovery.candidates_path), f, &err)) {
          cands = std::move(f);
          cands_mtime = m;
          std::ofstream sf(seen_path, std::ios::app);
          size_t fresh = 0;
          for (const PoolDef& d : cands.pools) {
            std::string a = b58(d.address);
            if (seen.insert(a).second) {
              sf << a << "\n";
              if (seeded) {
                active[d.address] = now;
                ++fresh;
              }
            }
          }
          if (!seeded) std::printf("seeded %zu known pools; new pools are the ones that appear from now on\n", seen.size());
          seeded = true;
          new_total += fresh;
          if (fresh) std::printf("%zu new pools in the candidates file\n", fresh);
        }
      }
      // 2. Expire tracked pools; rebuild the universe when it changes.
      for (auto it = active.begin(); it != active.end();)
        it = now - it->second > int64_t(track_min) * 60'000 ? active.erase(it) : std::next(it);
      std::set<Pubkey> want;
      std::map<Pubkey, std::vector<const PoolDef*>> by_token;
      for (const PoolDef& d : cands.pools) {
        if (!is_base(d.mint_a)) by_token[d.mint_a].push_back(&d);
        if (!is_base(d.mint_b)) by_token[d.mint_b].push_back(&d);
      }
      for (const auto& [addr, first] : active) {
        auto it = std::find_if(cands.pools.begin(), cands.pools.end(), [&](const PoolDef& d) { return d.address == addr; });
        if (it == cands.pools.end()) continue;
        size_t partners = 0;
        for (const Pubkey& tok : {it->mint_a, it->mint_b}) {
          if (is_base(tok)) continue;
          for (const PoolDef* p : by_token[tok]) {
            want.insert(p->address);
            ++partners;
          }
        }
        want.insert(addr);
        if (first == now) newlog << now << "\t" << b58(addr) << "\t" << dex_name(it->kind) << "\t" << partners << "\n" << std::flush;
      }
      if (!want.empty()) {
        // The deepest pools between base mints: 3-hop routes through USDC/USDT, and base pricing.
        std::vector<std::pair<double, const PoolDef*>> base_pools;
        for (size_t i = 0; i < cands.pools.size(); ++i)
          if (is_base(cands.pools[i].mint_a) && is_base(cands.pools[i].mint_b))
            base_pools.emplace_back(i < cands.tvl_sol.size() ? cands.tvl_sol[i] : 0, &cands.pools[i]);
        std::sort(base_pools.begin(), base_pools.end(), [](auto& a, auto& b) { return a.first > b.first; });
        for (size_t i = 0; i < base_pools.size() && i < 4; ++i) want.insert(base_pools[i].second->address);
      }
      if (want != universe) {
        universe = want;
        rt.reset();
        dlog.reset();
        if (!universe.empty()) {
          app::PoolsFile pf;
          for (size_t i = 0; i < cands.pools.size(); ++i)
            if (universe.count(cands.pools[i].address)) {
              pf.pools.push_back(cands.pools[i]);
              pf.tvl_sol.push_back(i < cands.tvl_sol.size() ? cands.tvl_sol[i] : 0);
            }
          rt = std::make_unique<app::Runtime>(cfg, pf);
          dlog = std::make_unique<obs::DecisionLog>(cfg.obs.decision_log_dir, cfg.obs.decision_log_rotate_s);
          rt->set_decision_log(dlog.get());
          for (const Pubkey& m : bases) rt->engine().set_own_balance(m, uint64_t(1) << 50);  // notional: shadow only
          keys = rt->store().subscribed_keys();
          ++rebuilds;
        }
      }
      // 3. Poll: read every key at one recent slot, then let the engine price what changed.
      if (rt) {
        try {
          rt->clock().slot.store(rpc.slot("processed"));
          feed::resync(rpc, rt->store(), keys, true);
          rt->evaluate_changed();
          ++polls;
        } catch (const std::exception& e) {
          std::fprintf(stderr, "poll: %s\n", e.what());
          std::this_thread::sleep_for(std::chrono::seconds(2));
        }
      }
      if (std::chrono::steady_clock::now() - last_status >= std::chrono::minutes(10)) {
        last_status = std::chrono::steady_clock::now();
        std::printf("[%s] new pools so far %llu, tracking %zu, universe %zu pools (rebuilt %llu times), %zu cycles, polls %llu, candidates %llu\n",
                    std::to_string(now / 1000).c_str(), (unsigned long long)new_total, active.size(), universe.size(), (unsigned long long)rebuilds,
                    rt ? rt->cycles().size() : size_t(0), (unsigned long long)polls,
                    rt ? (unsigned long long)rt->engine().counters().candidates : 0ULL);
        std::fflush(stdout);
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(rt ? poll_ms : 5000));
    }
    obs::Logger::get().stop();
    return 0;
  } catch (const std::exception& e) {
    std::fprintf(stderr, "newpool-shadow: %s\n", e.what());
    return 1;
  }
}
