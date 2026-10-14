#pragma once

#include "bpmm/risk.hpp"
#include "bpmm/strategy.hpp"

#include <cstdint>

namespace bpmm {

struct RuntimeConfig final {
  StrategyConfig strategy{};
  RiskLimits risk{};
  std::uint64_t shadow_latency_ns{5'000'000};
  std::int64_t maker_fee_ppm{200};
};

RuntimeConfig load_runtime_config();

}  // namespace bpmm
