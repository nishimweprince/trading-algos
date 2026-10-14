#pragma once

#include "bpmm/book.hpp"
#include "bpmm/strategy.hpp"

#include <array>
#include <cstdint>
#include <span>

namespace bpmm {

enum class ShadowOrderState : std::uint8_t { empty, pending, resting, partial, filled, canceled, rejected };

struct ShadowOrder final {
  std::uint64_t id{};
  Side side{Side::buy};
  std::uint8_t level{};
  Price price{};
  Quantity original{};
  Quantity remaining{};
  Quantity queue_ahead{};
  std::uint64_t arrival_ns{};
  ShadowOrderState state{ShadowOrderState::empty};
};

struct ShadowFill final {
  std::uint64_t order_id{};
  Side side{Side::buy};
  Price price{};
  Quantity quantity{};
  std::int64_t fee_raw{};
  std::uint64_t fill_ns{};
};

class ShadowVenue final {
 public:
  static constexpr std::size_t kMaxOrders = 16;
  static constexpr std::size_t kMaxFills = 32;

  explicit ShadowVenue(std::uint64_t one_way_latency_ns = 5'000'000,
                       std::int64_t maker_fee_ppm = 200)
      : latency_ns_(one_way_latency_ns), maker_fee_ppm_(maker_fee_ppm) {}

  void submit(const QuoteIntent& intent, const OrderBook& book) noexcept;
  void advance(std::uint64_t now_ns, const OrderBook& book) noexcept;
  void on_trade(const TradeEvent& trade) noexcept;
  void close_position(const PositionState& position, const OrderBook& book,
                      std::uint64_t now_ns) noexcept;
  void cancel_all() noexcept;
  [[nodiscard]] std::span<const ShadowFill> fills() const noexcept {
    return {fills_.data(), fill_count_};
  }
  void clear_fills() noexcept { fill_count_ = 0; }
  [[nodiscard]] const std::array<ShadowOrder, kMaxOrders>& orders() const noexcept { return orders_; }
  [[nodiscard]] std::uint32_t open_orders() const noexcept;
  [[nodiscard]] std::uint64_t oldest_pending_ns() const noexcept;

 private:
  ShadowOrder* find_slot(Side side, std::uint8_t level) noexcept;
  void fill(ShadowOrder& order, Quantity quantity, std::uint64_t fill_ns) noexcept;

  std::array<ShadowOrder, kMaxOrders> orders_{};
  std::array<ShadowFill, kMaxFills> fills_{};
  std::size_t fill_count_{};
  std::uint64_t next_order_id_{1};
  std::uint64_t latency_ns_;
  std::int64_t maker_fee_ppm_;
};

}  // namespace bpmm
