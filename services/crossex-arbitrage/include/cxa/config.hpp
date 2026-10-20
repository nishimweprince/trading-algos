#pragma once

#include "cxa/types.hpp"

#include <array>
#include <cstdint>
#include <string>

namespace cxa {

constexpr std::size_t kCensusSizes = 4;
constexpr std::size_t kCensusThresholds = 4;

enum class ExecMode : std::uint8_t { taker_taker, maker_taker };

struct StrategyConfig final {
  // Executable-gap census sizes (USD notional per leg) and thresholds.
  std::array<std::int64_t, kCensusSizes> census_sizes_raw{250 * kScale, 1'000 * kScale, 5'000 * kScale,
                                                          25'000 * kScale};
  std::array<std::int64_t, kCensusThresholds> census_thresholds_ppm{3'000, 4'000, 5'000, 7'500};
  std::int64_t trade_notional_raw{250 * kScale};
  std::uint64_t basis_halflife_ms{1'800'000};
  std::uint64_t basis_warmup_ms{600'000};
  std::uint64_t basis_sample_ms{1'000};
  std::int64_t min_net_ppm{0};            // expected net after fees+funding to enter
  std::int64_t safety_ppm{0};
  std::int64_t exit_band_ppm{200};        // dislocation vs entry mean that counts as converged
  std::uint64_t expected_hold_ms{600'000};
  std::uint64_t max_hold_ms{1'800'000};
  std::uint64_t funding_guard_ms{120'000};
  std::int64_t funding_tolerance_ppm{100};
  std::int64_t pair_stop_ppm{15'000};     // marked loss that forces an exit
  std::int64_t close_slippage_ppm{5'000};
  std::int64_t hedge_slippage_ppm{20'000};
  std::uint64_t cooldown_ms{5'000};
  std::int64_t max_mid_divergence_ppm{10'000};
  std::uint32_t max_repair_attempts{20};
  ExecMode exec_mode{ExecMode::maker_taker};
  std::uint64_t maker_timeout_ms{2'000};
};

struct RiskLimits final {
  std::uint32_t max_open_pairs{3};
  std::int64_t max_pair_notional_raw{5'000 * kScale};
  std::int64_t daily_loss_raw{200 * kScale};
  std::uint64_t max_feed_age_ns{1'000'000'000};
  std::uint64_t max_book_age_ns{60'000'000'000};
};

enum class LatencyMode : std::uint8_t { fixed, sampled, pessimistic };

struct ShadowConfig final {
  LatencyMode latency_mode{LatencyMode::sampled};
  // One-way send latency per venue when no RTT samples exist (Tokyo host defaults).
  std::array<std::uint64_t, kVenues> fixed_one_way_ns{2'000'000, 35'000'000};
  std::uint64_t engine_penalty_ns{1'000'000};
  std::int64_t available_ppm{500'000};  // share of displayed size a taker gets
  std::uint64_t seed{42};
  std::array<std::int64_t, kVenues> fallback_taker_ppm{500, 550};
  std::array<std::int64_t, kVenues> fallback_maker_ppm{200, 200};
};

struct RuntimeConfig final {
  std::string basket{
      "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,1000PEPEUSDT,SUIUSDT,WIFUSDT,AVAXUSDT,LINKUSDT,"
      "ADAUSDT,LTCUSDT,ARBUSDT,OPUSDT,NEARUSDT,APTUSDT,INJUSDT,TIAUSDT,SEIUSDT,FILUSDT,ATOMUSDT,"
      "ORDIUSDT,1000BONKUSDT,WLDUSDT,JUPUSDT,PENDLEUSDT,ENAUSDT,BCHUSDT,DOTUSDT,UNIUSDT"};
  StrategyConfig strategy{};
  RiskLimits risk{};
  ShadowConfig shadow{};
};

RuntimeConfig load_runtime_config();
std::string_view latency_mode_name(LatencyMode mode) noexcept;
std::string_view exec_mode_name(ExecMode mode) noexcept;

}  // namespace cxa
