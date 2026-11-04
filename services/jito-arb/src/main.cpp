// jito-arb: Solana atomic cyclic-arbitrage bot (spec services/jito-arb/docs/jito-arb.md).
//
//   jito-arb --config FILE [--dry-run] [--mode live|recorder|replay] [--replay FILE] [--pools FILE]
//            [--decisions FILE] [--run-seconds N]
//
// replay: feeds a recorded frame file through the same StateStore and engine as the live feed and writes
// a decision log (--decisions FILE, else rotated files under obs.decision_log_dir), then prints the report.
//
// dry_run is on unless the config file says `dry_run = false` and --dry-run is absent.
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <string>
#include <thread>

#include <fstream>
#include <sstream>

#include "app/bundle_builder.hpp"
#include "app/live.hpp"
#include "app/pools_file.hpp"
#include "app/runtime.hpp"
#include "core/base58.hpp"
#include "core/config.hpp"
#include "core/crypto.hpp"
#include "obs/decision_log.hpp"
#include "obs/log.hpp"
#include "obs/metrics.hpp"
#include "obs/replay_report.hpp"

namespace {

std::atomic<bool> g_stop{false};
void on_signal(int) { g_stop = true; }

struct Args {
  std::string config, mode = "live", replay, pools, decisions;
  bool dry_run_flag = false;
  int run_seconds = 0;  // 0 = until SIGINT/SIGTERM
};

bool parse_args(int argc, char** argv, Args& a) {
  for (int i = 1; i < argc; ++i) {
    std::string s = argv[i];
    auto next = [&](std::string& out) {
      if (i + 1 >= argc) return false;
      out = argv[++i];
      return true;
    };
    if (s == "--config") { if (!next(a.config)) return false; }
    else if (s == "--mode") { if (!next(a.mode)) return false; }
    else if (s == "--replay") { if (!next(a.replay)) return false; }
    else if (s == "--pools") { if (!next(a.pools)) return false; }
    else if (s == "--decisions") { if (!next(a.decisions)) return false; }
    else if (s == "--dry-run") a.dry_run_flag = true;
    else if (s == "--run-seconds") { std::string v; if (!next(v)) return false; a.run_seconds = std::stoi(v); }
    else return false;
  }
  return a.mode == "live" || a.mode == "recorder" || a.mode == "replay";
}

int run_replay(const arb::Config& cfg, const Args& args) {
  std::string pools_path = args.pools.empty() ? cfg.pools_path : args.pools;
  std::ifstream f(pools_path);
  if (!f) {
    LOG_ERROR("replay: cannot read pools file %s", pools_path.c_str());
    return 1;
  }
  std::stringstream ss;
  ss << f.rdbuf();
  arb::app::PoolsFile pf;
  std::string err;
  if (!arb::app::from_json(ss.str(), pf, &err)) {
    LOG_ERROR("replay: %s", err.c_str());
    return 1;
  }
  arb::app::Runtime rt(cfg, pf);
  arb::obs::DecisionLog log(cfg.obs.decision_log_dir, cfg.obs.decision_log_rotate_s);
  if (!args.decisions.empty() && !log.open_path(args.decisions)) {
    LOG_ERROR("replay: cannot write %s", args.decisions.c_str());
    return 1;
  }
  rt.set_decision_log(&log);
  // Dry-run bundles: built, sign-guarded and signed exactly as live, then recorded; the Submitter has no
  // client in dry run, so nothing can be sent. Replay is always dry run.
  arb::Keypair wallet = arb::app::dry_run_keypair();
  if (::access(cfg.keys.hot_wallet_path.c_str(), R_OK) == 0) {
    try {
      wallet = arb::Keypair::load_file(cfg.keys.hot_wallet_path);
    } catch (const std::exception& e) {
      LOG_ERROR("hot wallet: %s", e.what());
      return 1;
    }
  } else {
    LOG_WARN("replay: no hot wallet file at %s; signing with the fixed dry-run test key", cfg.keys.hot_wallet_path.c_str());
  }
  arb::Pubkey executor = cfg.keys.executor_program_id == "VERIFY" ? arb::app::dry_run_executor()
                                                                  : arb::parse_pubkey(cfg.keys.executor_program_id);
  arb::tx::AltManager alts;
  arb::tx::BlockhashCache blockhash;
  arb::risk::Limits limits(arb::risk::limits_config(cfg));
  arb::submit::Submitter submitter(true, nullptr);
  arb::app::BundleBuilder builder(cfg, rt, wallet, executor, alts, blockhash, limits, submitter);
  builder.set_offline(true);
  rt.set_send_hook([&](const arb::engine::Opportunity& o, const arb::engine::Cycle& c, arb::obs::DecisionRecord& rec) { builder(o, c, rec); });
  LOG_INFO("replay: %zu pools, %zu cycles, frames %s", pf.pools.size(), rt.cycles().size(), args.replay.c_str());
  auto t0 = std::chrono::steady_clock::now();
  if (!rt.replay_file(args.replay)) {
    LOG_ERROR("replay: cannot open %s", args.replay.c_str());
    return 1;
  }
  double secs = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
  const auto& st = rt.stats();
  const auto& n = rt.engine().counters();
  LOG_INFO("replay done in %.3fs: frames=%llu bad=%llu pool_updates=%llu cycles_seen=%llu stale=%llu unsupported=%llu prefilter_pass=%llu sized=%llu "
           "candidates=%llu dispatched=%llu records=%llu (dry run: nothing sent)",
           secs, (unsigned long long)st.frames, (unsigned long long)st.bad_frames, (unsigned long long)st.updates,
           (unsigned long long)n.cycles_seen, (unsigned long long)n.stale, (unsigned long long)n.unsupported,
           (unsigned long long)n.prefilter_pass,
           (unsigned long long)n.sized, (unsigned long long)n.candidates, (unsigned long long)st.sent,
           (unsigned long long)st.records);
  const auto& b = builder.stats();
  LOG_INFO("bundles: built=%llu signed=%llu dry_run=%llu too_large=%llu guard_blocked=%llu risk_blocked=%llu failed=%llu sent=%llu",
           (unsigned long long)b.built, (unsigned long long)b.signed_ok, (unsigned long long)b.dry_run,
           (unsigned long long)b.too_large, (unsigned long long)b.guard_blocked, (unsigned long long)b.risk_blocked,
           (unsigned long long)b.build_failed, (unsigned long long)b.sent);
  std::string path = log.current_path();
  log.close();
  if (!path.empty()) {
    auto rep = arb::obs::aggregate(arb::obs::read_records({path}), int64_t(cfg.engine.min_ev_lamports), cfg.engine.min_net_lamports);
    std::fputs(arb::obs::to_text(rep).c_str(), stdout);
  }
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  Args args;
  if (!parse_args(argc, argv, args)) {
    std::fprintf(stderr, "usage: jito-arb --config FILE [--dry-run] [--mode live|recorder|replay] [--replay FILE] [--run-seconds N]\n");
    return 2;
  }
  arb::Config cfg;
  try {
    cfg = arb::load_config(args.config);
  } catch (const std::exception& e) {
    std::fprintf(stderr, "config: %s\n", e.what());
    return 2;
  }
  if (args.dry_run_flag) cfg.mode.dry_run = true;
  if (args.mode == "replay") cfg.mode.dry_run = true;  // replay never sends
  arb::crypto_init();
  auto& log = arb::obs::Logger::get();
  log.start(cfg.obs.log_path, arb::obs::parse_level(cfg.obs.log_level));
  std::signal(SIGINT, on_signal);
  std::signal(SIGTERM, on_signal);

  auto& reg = arb::obs::Registry::get();
  reg.gauge("arb_dry_run", "1 when the bot never sends bundles").set(cfg.mode.dry_run ? 1 : 0);
  reg.gauge("arb_kill_switch", "1 while the KILL file is present").set(0);
  arb::obs::MetricsServer metrics;
  std::string err;
  if (!metrics.start(cfg.obs.metrics_addr, cfg.obs.metrics_port, &err)) {
    LOG_ERROR("metrics: %s", err.c_str());
    log.stop();
    return 1;
  }
  LOG_INFO("jito-arb starting: mode=%s dry_run=%d funding=%s metrics=%s:%d", args.mode.c_str(), int(cfg.mode.dry_run),
           cfg.mode.funding_mode.c_str(), cfg.obs.metrics_addr.c_str(), metrics.port());

  int rc = 0;
  if (args.mode == "replay") {
    if (args.replay.empty()) {
      LOG_ERROR("--mode replay needs --replay FILE");
      rc = 2;
    } else {
      rc = run_replay(cfg, args);
    }
    metrics.stop();
    log.stop();
    return rc;
  }
  if (args.mode == "live" || args.mode == "recorder") {
#if !defined(ARB_WITH_GRPC)
    LOG_WARN("built without ARB_WITH_GRPC: the LaserStream stub yields no frames (the feed is a VM build)");
#endif
    rc = arb::app::run_live(cfg, args.mode == "recorder", g_stop, args.run_seconds);
    metrics.stop();
    log.stop();
    return rc;
  }
  auto t0 = std::chrono::steady_clock::now();
  auto& kill_g = reg.gauge("arb_kill_switch", "");
  while (!g_stop) {
    kill_g.set(::access(cfg.mode.kill_file.c_str(), F_OK) == 0 ? 1 : 0);
    if (args.run_seconds > 0 && std::chrono::steady_clock::now() - t0 >= std::chrono::seconds(args.run_seconds)) break;
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }
  LOG_INFO("jito-arb stopping");
  metrics.stop();
  log.stop();
  return rc;
}
