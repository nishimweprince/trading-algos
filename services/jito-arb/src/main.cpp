// jito-arb: Solana atomic cyclic-arbitrage bot (spec services/jito-arb/docs/jito-arb.md).
//
//   jito-arb --config FILE [--dry-run] [--mode live|recorder|replay] [--replay FILE] [--run-seconds N]
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

#include "core/config.hpp"
#include "core/crypto.hpp"
#include "obs/log.hpp"
#include "obs/metrics.hpp"

namespace {

std::atomic<bool> g_stop{false};
void on_signal(int) { g_stop = true; }

struct Args {
  std::string config, mode = "live", replay;
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
    else if (s == "--dry-run") a.dry_run_flag = true;
    else if (s == "--run-seconds") { std::string v; if (!next(v)) return false; a.run_seconds = std::stoi(v); }
    else return false;
  }
  return a.mode == "live" || a.mode == "recorder" || a.mode == "replay";
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
  if (args.mode == "live" || args.mode == "recorder") {
#if !defined(ARB_WITH_GRPC)
    LOG_WARN("built without ARB_WITH_GRPC: no LaserStream feed; serving metrics only");
#endif
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
