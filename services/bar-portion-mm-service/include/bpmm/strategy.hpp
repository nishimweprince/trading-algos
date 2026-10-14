#pragma once

#include "bpmm/book.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <vector>

namespace bpmm {

struct StrategyConfig final {
  Price tick_size{Decimal::parse("0.01")};
  Quantity quantity_step{Decimal::parse("0.001")};
  Quantity quote_quantity{Decimal::parse("0.01")};
  std::int64_t base_spread_ppm{400};       // 4 bp from reference on each side
  std::int64_t level_step_ppm{200};        // 2 bp
  std::int64_t forecast_clamp_ppm{500};    // 5 bp
  std::int64_t inventory_skew_ppm{300};    // at max inventory
  std::int64_t reprice_threshold_ticks{1};
  std::int64_t max_inventory_raw{10 * kScale};
  std::int64_t stop_loss_ppm{2'000};       // 20 bp
  std::int64_t take_profit_ppm{2'000};     // 20 bp
  std::uint64_t time_barrier_ms{300'000};  // 5 min, project default
  std::uint64_t cooldown_ms{480'000};      // 8 min, conservative paper-adjacent default
  std::uint64_t max_quote_age_ns{5'000'000'000ULL};
  std::int64_t volatility_spread_multiplier{4};
  std::uint8_t levels{2};
};

struct Regression final {
  double intercept{};
  double slope{-0.0001};
  bool calibrated{};

  [[nodiscard]] double predict(double bp) const noexcept { return intercept + slope * bp; }
};

class RollingRegression final {
 public:
  explicit RollingRegression(std::size_t capacity = 51'840);
  void push(double x, double y) noexcept;
  [[nodiscard]] Regression fit() const noexcept;
  [[nodiscard]] std::size_t size() const noexcept { return size_; }

 private:
  struct Observation { double x; double y; };
  std::vector<Observation> values_;
  std::size_t head_{};
  std::size_t size_{};
  double sx_{};
  double sy_{};
  double sxx_{};
  double sxy_{};
};

class BarPortion final {
 public:
  bool on_closed(const CandleEvent& candle) noexcept;
  [[nodiscard]] double value() const noexcept { return value_; }
  [[nodiscard]] std::uint64_t close_ms() const noexcept { return close_ms_; }
  [[nodiscard]] bool ready() const noexcept { return ready_; }
  [[nodiscard]] std::int64_t volatility_ppm() const noexcept { return volatility_ppm_; }

 private:
  double value_{};
  std::uint64_t close_ms_{};
  bool ready_{};
  Price previous_close_{};
  std::int64_t volatility_ppm_{};
};

struct PositionState final {
  std::int64_t quantity_raw{};  // positive long, negative short
  Price average_entry{};
  std::uint64_t opened_ms{};
  Price favorable_extreme{};
};

class QuoteStrategy final {
 public:
  explicit QuoteStrategy(StrategyConfig config = {}) : config_(config) {}

  [[nodiscard]] IntentBatch quote(const OrderBook& book, const BarPortion& bp,
                                  const Regression& regression, const PositionState& position,
                                  std::uint64_t now_ns) noexcept;
  [[nodiscard]] bool barrier_exit(const PositionState& position, Price mid,
                                  std::uint64_t now_ms) const noexcept;
  void accepted(const QuoteIntent& intent) noexcept;
  [[nodiscard]] const StrategyConfig& config() const noexcept { return config_; }

 private:
  StrategyConfig config_;
  std::array<Price, 2> active_bid_{};
  std::array<Price, 2> active_ask_{};
  std::array<std::uint64_t, 2> active_bid_ns_{};
  std::array<std::uint64_t, 2> active_ask_ns_{};
  std::uint64_t sequence_{};
};

}  // namespace bpmm
