#pragma once

#include "cxa/types.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>

namespace cxa {

enum class BookState : std::uint8_t { empty, syncing, verified, gap };

struct SweepResult final {
  Quantity filled{};
  std::int64_t notional_raw{};  // sum(price * qty), USD scaled by kScale
  Price vwap{};
  Price worst{};                // last level touched
  [[nodiscard]] bool complete(Quantity wanted) const noexcept { return filled.raw >= wanted.raw; }
};

// Fixed-capacity L2 book shared by both venues. Levels beyond capacity are truncated
// from the far end: deep levels never decide a sweep at the sizes this service trades.
class OrderBook final {
 public:
  static constexpr std::size_t kCapacity = 512;

  // `verified` is true for Bybit, whose snapshot is self-sufficient; Binance snapshots
  // stay `syncing` until the first bridging diff arrives.
  bool apply_snapshot(std::uint64_t last_update_id, std::span<const Level> bids,
                      std::span<const Level> asks, std::uint64_t receive_ns, bool verified) noexcept;
  bool apply_binance(const DepthEvent& event) noexcept;
  bool apply_bybit(const DepthEvent& event) noexcept;
  void mark_gap() noexcept { state_ = BookState::gap; }
  void reset(BookState state = BookState::syncing) noexcept;

  [[nodiscard]] BookState state() const noexcept { return state_; }
  [[nodiscard]] bool verified() const noexcept { return state_ == BookState::verified; }
  [[nodiscard]] std::uint64_t last_update_id() const noexcept { return last_update_id_; }
  [[nodiscard]] std::uint64_t last_receive_ns() const noexcept { return last_receive_ns_; }
  // Exchange timestamp (ms) of the last applied venue event; 0 after a REST snapshot.
  [[nodiscard]] std::uint64_t event_ms() const noexcept { return event_ms_; }
  [[nodiscard]] Level best_bid() const noexcept;
  [[nodiscard]] Level best_ask() const noexcept;
  [[nodiscard]] Price mid() const noexcept;
  [[nodiscard]] bool uncrossed() const noexcept;
  [[nodiscard]] std::size_t depth(Side side) const noexcept { return side == Side::buy ? bid_size_ : ask_size_; }

  // Takes liquidity as a taker on `taker_side` (buy walks asks). Stops at `limit`
  // (0 = no limit) and counts only `available_ppm` of each displayed level.
  [[nodiscard]] SweepResult sweep(Side taker_side, Quantity quantity, Price limit = {},
                                  std::int64_t available_ppm = kPpm) const noexcept;
  [[nodiscard]] std::uint64_t state_hash() const noexcept;

 private:
  using Levels = std::array<Level, kCapacity>;
  static bool update(Levels& levels, std::size_t& size, Level level, bool descending) noexcept;
  bool apply_changes(const DepthEvent& event) noexcept;

  Levels bids_{};
  Levels asks_{};
  std::size_t bid_size_{};
  std::size_t ask_size_{};
  std::uint64_t last_update_id_{};
  std::uint64_t last_receive_ns_{};
  std::uint64_t event_ms_{};
  BookState state_{BookState::empty};
};

}  // namespace cxa
