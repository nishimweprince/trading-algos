#pragma once

#include <array>
#include <cmath>
#include <cstdint>
#include <optional>

// One L2 book, as pk-core's `Book`: price keys are ticks of 0.0001 (Kalshi
// quotes deci-cents), sizes are kept to 6 dp (summed fractional Kalshi deltas
// otherwise leave ghost levels that cross the book), and a crossed, one-sided
// or unsnapshotted book has no quote. Fixed arrays, so updates never allocate.
namespace pkm {

inline constexpr int kKeys = 10'001;  // 0.0000 … 1.0000
inline int px_key(double price) noexcept { return static_cast<int>(std::lround(price * 10'000.0)); }
inline double key_px(int key) noexcept { return static_cast<double>(key) / 10'000.0; }
inline double round_size(double size) noexcept { return std::round(size * 1e6) / 1e6; }

enum class Side : std::uint8_t { bid, ask };

struct Quote {
  bool valid = false;
  double bid = 0;
  double ask = 0;
  [[nodiscard]] std::optional<double> mid() const noexcept {
    return valid ? std::optional<double>((bid + ask) / 2.0) : std::nullopt;
  }
};

class Book final {
 public:
  void begin_snapshot(std::int64_t recv_ns) noexcept;  // clears both sides
  void set(Side side, int key, double size, std::int64_t recv_ns) noexcept;
  void add(Side side, int key, double delta, std::int64_t recv_ns) noexcept;
  void invalidate() noexcept;

  [[nodiscard]] bool has_snapshot() const noexcept { return snapshot_ns_ != 0; }
  [[nodiscard]] std::int64_t last_update_ns() const noexcept { return last_update_ns_; }
  [[nodiscard]] int best_bid_key() const noexcept;  // -1 none
  [[nodiscard]] int best_ask_key() const noexcept;
  [[nodiscard]] std::optional<double> best_bid() const noexcept;
  [[nodiscard]] std::optional<double> best_ask() const noexcept;
  [[nodiscard]] double size_at(Side side, int key) const noexcept;
  // Snapshot seen, both sides present, not crossed.
  [[nodiscard]] Quote quote() const noexcept;

 private:
  struct Levels {
    std::array<double, kKeys> size{};
    std::array<std::uint64_t, (kKeys + 63) / 64> used{};
    int count = 0;
    void put(int key, double value) noexcept;
    void clear() noexcept;
    [[nodiscard]] int highest() const noexcept;
    [[nodiscard]] int lowest() const noexcept;
  };
  Levels& levels(Side side) noexcept { return side == Side::bid ? bids_ : asks_; }

  Levels bids_;
  Levels asks_;
  std::int64_t snapshot_ns_ = 0;
  std::int64_t last_update_ns_ = 0;
};

}  // namespace pkm
