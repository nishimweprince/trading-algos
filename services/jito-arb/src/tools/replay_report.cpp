// replay-report: per-day detections from decision logs (spec §13 phase 2 report).
//
//   replay-report [--config FILE] [--json] DECISION_LOG...
//
// min_ev_lamports and min_net_lamports come from the config (defaults otherwise). The comparison with
// arbs that actually landed on chain in the same slots needs block data: a VM step (RUNBOOK-VM.md).
#include <cstdio>
#include <string>
#include <vector>

#include "core/config.hpp"
#include "obs/replay_report.hpp"

int main(int argc, char** argv) {
  arb::Config cfg;
  bool json = false;
  std::vector<std::string> files;
  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    if (a == "--config" && i + 1 < argc) {
      try {
        cfg = arb::load_config(argv[++i]);
      } catch (const std::exception& e) {
        std::fprintf(stderr, "config: %s\n", e.what());
        return 2;
      }
    } else if (a == "--json") {
      json = true;
    } else {
      files.push_back(a);
    }
  }
  if (files.empty()) {
    std::fprintf(stderr, "usage: replay-report [--config FILE] [--json] DECISION_LOG...\n");
    return 2;
  }
  auto recs = arb::obs::read_records(files);
  auto rep = arb::obs::aggregate(recs, int64_t(cfg.engine.min_ev_lamports), cfg.engine.min_net_lamports);
  std::fputs((json ? arb::obs::to_json(rep) + "\n" : arb::obs::to_text(rep)).c_str(), stdout);
  bool gate = false;
  for (auto& [day, d] : rep.days) gate |= d.gate_passes >= 20;
  std::fprintf(stderr, "phase-2 gate (>= 20 detections/day above min_ev after the policy tip): %s\n",
               gate ? "met on at least one day" : "not met");
  return 0;
}
