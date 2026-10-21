#pragma once

#include <atomic>
#include <cstdint>

namespace hla {

enum class Mode : std::uint8_t {
  check,    // every startup check, read-only, then exit
  dry_run,  // full pipeline; HL acks and fills and MT5 hedges are simulated, nothing is sent
  live,
};

struct RunOptions final {
  Mode mode{Mode::check};
  std::uint64_t duration_s{};  // 0: until stopped
  bool sim_mt5_from_hl{};      // dry-run without a terminal: MT5 ticks mirror the HL book
};

// Startup preflight, then the trading loop until `stop` or `duration_s`. Returns the exit code.
int run_trader(const RunOptions& options, std::atomic<bool>& stop);

}  // namespace hla
