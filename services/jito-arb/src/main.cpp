// jito-arb: Solana atomic cyclic-arbitrage bot (spec services/jito-arb/docs/jito-arb.md).
//
//   jito-arb --config FILE [--mode live|recorder] [--run-seconds N]
//
// live: trades. There is no dry run (removed 17 Nov 2026): every precondition is checked at start (app/live.cpp)
// and anything missing refuses the start. recorder: writes raw LaserStream frames to disk and never builds or
// sends anything. Offline frame replay stays a library entry point (Runtime::replay_file) for tests and arb_bench.
#include <atomic>
#include <csignal>
#include <cstdio>
#include <string>

#include "app/live.hpp"
#include "core/config.hpp"
#include "core/crypto.hpp"
#include "obs/log.hpp"
#include "obs/metrics.hpp"

namespace {

std::atomic<bool> g_stop{false};
void on_signal(int) { g_stop = true; }

struct Args {
  std::string config, mode = "live";
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
    else if (s == "--run-seconds") { std::string v; if (!next(v)) return false; a.run_seconds = std::stoi(v); }
    else if (s == "--dry-run") {
      std::fprintf(stderr, "--dry-run was removed: jito-arb is live only (use the KILL file to stop sending)\n");
      return false;
    }
    else return false;
  }
  return a.mode == "live" || a.mode == "recorder";
}

}  // namespace

int main(int argc, char** argv) {
  Args args;
  if (!parse_args(argc, argv, args)) {
    std::fprintf(stderr, "usage: jito-arb --config FILE [--mode live|recorder] [--run-seconds N]\n");
    return 2;
  }
  arb::Config cfg;
  try {
    cfg = arb::load_config(args.config);
  } catch (const std::exception& e) {
    std::fprintf(stderr, "config: %s\n", e.what());
    return 2;
  }
  if (cfg.mode.dry_run) {
    std::fprintf(stderr, "config: dry_run = true is no longer supported (jito-arb is live only since 17 Nov 2026); "
                         "remove the key, and use the KILL file to stop sending\n");
    return 2;
  }
  arb::crypto_init();
  auto& log = arb::obs::Logger::get();
  log.start(cfg.obs.log_path, arb::obs::parse_level(cfg.obs.log_level));
  std::signal(SIGINT, on_signal);
  std::signal(SIGTERM, on_signal);

  auto& reg = arb::obs::Registry::get();
  reg.gauge("arb_kill_switch", "1 while the KILL file is present").set(0);
  arb::obs::MetricsServer metrics;
  std::string err;
  if (!metrics.start(cfg.obs.metrics_addr, cfg.obs.metrics_port, &err)) {
    LOG_ERROR("metrics: %s", err.c_str());
    log.stop();
    return 1;
  }
  LOG_INFO("jito-arb starting: mode=%s funding=%s metrics=%s:%d", args.mode.c_str(), cfg.mode.funding_mode.c_str(),
           cfg.obs.metrics_addr.c_str(), metrics.port());
#if !defined(ARB_WITH_GRPC)
  LOG_WARN("built without ARB_WITH_GRPC: the LaserStream stub yields no frames (the feed is a VM build)");
#endif
  int rc = arb::app::run_live(cfg, args.mode == "recorder", g_stop, args.run_seconds);
  metrics.stop();
  log.stop();
  return rc;
}
