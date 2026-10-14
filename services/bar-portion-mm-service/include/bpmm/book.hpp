#pragma once

#include "bpmm/types.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>

namespace bpmm {

class OrderBook final {
 public:
  static constexpr std::size_t kCapacity = 1024;

  bool apply_snapshot(std::uint64_t last_update_id, std::span<const Level> bids,
                      std::span<const Level> asks) noexcept;
  bool apply(const DepthEvent& event) noexcept;
  void reset(BookState state = BookState::syncing) noexcept;

  [[nodiscard]] BookState state() const noexcept { return state_; }
  [[nodiscard]] std::uint64_t last_update_id() const noexcept { return last_update_id_; }
  [[nodiscard]] Level best_bid() const noexcept;
  [[nodiscard]] Level best_ask() const noexcept;
  [[nodiscard]] Quantity quantity_at(Side side, Price price) const noexcept;
  [[nodiscard]] bool uncrossed() const noexcept;
  [[nodiscard]] std::uint64_t state_hash() const noexcept;

 private:
  using Levels = std::array<Level, kCapacity>;
  static bool update(Levels& levels, std::size_t& size, Level level, bool descending) noexcept;
  static void sort(Levels& levels, std::size_t size, bool descending) noexcept;

  Levels bids_{};
  Levels asks_{};
  std::size_t bid_size_{};
  std::size_t ask_size_{};
  std::uint64_t last_update_id_{};
  BookState state_{BookState::empty};
};

}  // namespace bpmm
