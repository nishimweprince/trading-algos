#pragma once

#include "bpmm/book.hpp"
#include "bpmm/risk.hpp"
#include "bpmm/shadow.hpp"
#include "bpmm/strategy.hpp"

#include <cstdint>

namespace bpmm {

struct Snapshot;

struct EngineMetrics final {
  std::uint64_t depth_events{};
  std::uint64_t trade_events{};
  std::uint64_t closed_candles{};
  std::uint64_t intents{};
  std::uint64_t risk_rejects{};
  std::uint64_t fills{};
  std::uint64_t gaps{};
  std::int64_t realized_pnl_raw{};
};

class Engine final {
 public:
  Engine(StrategyConfig strategy = {}, RiskLimits risk = {},
         std::uint64_t shadow_latency_ns = 5'000'000, std::int64_t maker_fee_ppm = 200);

  bool on_snapshot(const Snapshot& snapshot) noexcept;
  IntentBatch on_depth(const DepthEvent& event, std::uint64_t now_ns) noexcept;
  void on_trade(const TradeEvent& event) noexcept;
  bool on_candle(const CandleEvent& event) noexcept;
  void on_gap() noexcept;
  void kill() noexcept;
  void set_regression(Regression regression) noexcept { regression_ = regression; }
  void set_next_funding_ms(std::uint64_t value) noexcept { risk_state_.next_funding_ms = value; }
  void acknowledge_halt() noexcept;

  [[nodiscard]] const OrderBook& book() const noexcept { return book_; }
  [[nodiscard]] const BarPortion& bar_portion() const noexcept { return bp_; }
  [[nodiscard]] const PositionState& position() const noexcept { return position_; }
  [[nodiscard]] const EngineMetrics& metrics() const noexcept { return metrics_; }
  [[nodiscard]] const ShadowVenue& venue() const noexcept { return venue_; }
  [[nodiscard]] std::span<const ShadowFill> fill_events() const noexcept {
    return {fill_events_.data(), fill_event_count_};
  }
  void clear_fill_events() noexcept { fill_event_count_ = 0; }
  [[nodiscard]] bool ready() const noexcept;
  [[nodiscard]] bool halted() const noexcept { return risk_.halted(); }
  [[nodiscard]] std::uint64_t state_hash() const noexcept;

 private:
  void apply_fills() noexcept;

  OrderBook book_;
  BarPortion bp_;
  Regression regression_{};
  QuoteStrategy strategy_;
  RiskEngine risk_;
  ShadowVenue venue_;
  PositionState position_{};
  RiskSnapshot risk_state_{};
  EngineMetrics metrics_{};
  std::array<ShadowFill, ShadowVenue::kMaxFills> fill_events_{};
  std::size_t fill_event_count_{};
  bool durable_halt_{};
  std::int64_t aggressive_buy_raw_{};
  std::int64_t aggressive_sell_raw_{};
  std::uint64_t flow_window_ms_{};
  std::uint64_t cooldown_until_ms_{};
};

}  // namespace bpmm
