#pragma once

#include "bpmm/types.hpp"

#include <array>
#include <cstdint>

namespace bpmm {

enum class RiskCode : std::uint8_t {
  ok,
  halted,
  stale_book,
  book_unverified,
  inventory_limit,
  order_notional_limit,
  open_order_limit,
  order_rate_limit,
  daily_loss_limit,
  drawdown_limit,
  spread_limit,
  funding_guard,
  reject_limit,
  toxicity_limit,
  leverage_limit,
  margin_limit,
  unacknowledged_order
};

struct RiskLimits final {
  std::int64_t max_inventory_raw{10 * kScale};
  std::int64_t max_order_notional_raw{1'000 * kScale};
  std::uint32_t max_open_orders{8};
  std::uint32_t max_orders_per_second{8};
  std::int64_t daily_loss_raw{50 * kScale};
  std::int64_t max_drawdown_raw{75 * kScale};
  std::int64_t max_spread_ppm{2'000};
  std::uint64_t max_book_age_ns{250'000'000};
  std::uint64_t funding_guard_ms{60'000};
  std::uint32_t max_consecutive_rejects{5};
  std::int64_t toxicity_limit_ppm{800'000};
  std::int64_t max_leverage_ppm{2'000'000};
  std::int64_t max_margin_utilization_ppm{800'000};
  std::uint64_t max_unacknowledged_ns{1'000'000'000};
};

struct RiskSnapshot final {
  bool halted{};
  bool book_verified{};
  std::uint64_t book_receive_ns{};
  std::uint64_t next_funding_ms{};
  std::int64_t inventory_raw{};
  std::int64_t realized_pnl_raw{};
  std::int64_t equity_raw{};
  std::int64_t peak_equity_raw{};
  std::uint32_t open_orders{};
  std::uint32_t consecutive_rejects{};
  std::int64_t toxicity_ppm{};  // positive aggressive buy flow
  std::uint64_t oldest_pending_ns{};
};

class RiskEngine final {
 public:
  explicit RiskEngine(RiskLimits limits = {}) : limits_(limits) {}
  [[nodiscard]] RiskCode check(const QuoteIntent& intent, Price best_bid, Price best_ask,
                               const RiskSnapshot& state, std::uint64_t now_ns,
                               std::uint64_t now_ms) noexcept;
  void halt() noexcept { halted_ = true; }
  void acknowledge() noexcept { halted_ = false; }
  [[nodiscard]] bool halted() const noexcept { return halted_; }

 private:
  RiskLimits limits_;
  std::array<std::uint64_t, 64> order_times_{};
  std::size_t order_time_head_{};
  bool halted_{};
};

}  // namespace bpmm
