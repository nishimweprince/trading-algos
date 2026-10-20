#pragma once

#include <array>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string_view>

namespace cxa {

constexpr std::int64_t kScale = 100'000'000;
constexpr std::int64_t kPpm = 1'000'000;

struct Decimal final {
  std::int64_t raw{};

  static Decimal from_double(double value) noexcept {
    return {static_cast<std::int64_t>(std::llround(value * static_cast<double>(kScale)))};
  }

  static Decimal parse(std::string_view text) {
    bool negative = false;
    if (!text.empty() && text.front() == '-') {
      negative = true;
      text.remove_prefix(1);
    }
    const auto dot = text.find('.');
    const auto whole_text = text.substr(0, dot);
    std::int64_t whole = 0;
    if (!whole_text.empty()) {
      const auto [ptr, ec] = std::from_chars(whole_text.data(), whole_text.data() + whole_text.size(), whole);
      if (ec != std::errc{} || ptr != whole_text.data() + whole_text.size()) {
        throw std::invalid_argument("invalid decimal");
      }
    }
    std::int64_t fraction = 0;
    std::int64_t place = kScale / 10;
    if (dot != std::string_view::npos) {
      auto digits = text.substr(dot + 1);
      for (char c : digits) {
        if (c < '0' || c > '9') throw std::invalid_argument("invalid decimal");
        if (place > 0) {
          fraction += static_cast<std::int64_t>(c - '0') * place;
          place /= 10;
        }
      }
    }
    const auto value = whole * kScale + fraction;
    return {negative ? -value : value};
  }

  [[nodiscard]] double as_double() const noexcept {
    return static_cast<double>(raw) / static_cast<double>(kScale);
  }

  friend constexpr auto operator<=>(Decimal, Decimal) = default;
};

using Price = Decimal;
using Quantity = Decimal;

enum class Side : std::uint8_t { buy, sell };

inline constexpr Side opposite(Side side) noexcept {
  return side == Side::buy ? Side::sell : Side::buy;
}

enum class Venue : std::uint8_t { binance = 0, bybit = 1 };
constexpr std::size_t kVenues = 2;
constexpr std::size_t kMaxSymbols = 32;

inline constexpr std::size_t index(Venue venue) noexcept { return static_cast<std::size_t>(venue); }
inline constexpr std::string_view venue_name(Venue venue) noexcept {
  return venue == Venue::binance ? "binance" : "bybit";
}

struct Level final {
  Price price{};
  Quantity quantity{};
};

// Binance @0ms diffs reach a few hundred levels and list far prices first, so a diff
// is never truncated: anything larger is treated as a gap.
constexpr std::size_t kMaxDepthChanges = 512;

// One book change on either venue. Bybit snapshots (L50) travel as a DepthEvent with
// `snapshot` set; Binance REST snapshots are larger and travel as BookSnapshot.
struct DepthEvent final {
  Venue venue{Venue::binance};
  std::uint16_t symbol{};
  bool snapshot{};
  std::uint64_t receive_ns{};
  std::uint64_t event_ms{};
  std::uint64_t first_id{};           // Binance U, Bybit u
  std::uint64_t final_id{};           // Binance u, Bybit u
  std::uint64_t previous_final_id{};  // Binance pu, unused on Bybit
  std::array<Level, kMaxDepthChanges> bids{};
  std::array<Level, kMaxDepthChanges> asks{};
  std::uint16_t bid_count{};
  std::uint16_t ask_count{};
};

struct FundingEvent final {
  Venue venue{Venue::binance};
  std::uint16_t symbol{};
  std::uint64_t receive_ns{};
  std::uint64_t event_ms{};
  std::uint64_t next_funding_ms{};  // 0 when the frame did not carry it
  Price mark{};                     // 0 when absent
  Decimal funding_rate{};           // per settlement, e.g. 0.0001
  bool has_rate{};
};

inline constexpr std::int64_t mul_scaled(std::int64_t lhs, std::int64_t rhs) noexcept {
  return static_cast<std::int64_t>((static_cast<__int128>(lhs) * rhs) / kScale);
}

inline constexpr std::int64_t div_scaled(std::int64_t lhs, std::int64_t rhs) noexcept {
  return rhs == 0 ? 0 : static_cast<std::int64_t>((static_cast<__int128>(lhs) * kScale) / rhs);
}

// (value * ppm) / 1e6 without overflow.
inline constexpr std::int64_t apply_ppm(std::int64_t value, std::int64_t ppm) noexcept {
  return static_cast<std::int64_t>((static_cast<__int128>(value) * ppm) / kPpm);
}

// numerator / denominator expressed in ppm.
inline constexpr std::int64_t ratio_ppm(std::int64_t numerator, std::int64_t denominator) noexcept {
  return denominator == 0 ? 0
                          : static_cast<std::int64_t>((static_cast<__int128>(numerator) * kPpm) / denominator);
}

inline constexpr Price round_down(Price value, Price tick) noexcept {
  return tick.raw <= 0 ? value : Price{(value.raw / tick.raw) * tick.raw};
}

inline constexpr Price round_up(Price value, Price tick) noexcept {
  if (tick.raw <= 0) return value;
  return Price{((value.raw + tick.raw - 1) / tick.raw) * tick.raw};
}

inline constexpr std::int64_t abs64(std::int64_t value) noexcept { return value < 0 ? -value : value; }

}  // namespace cxa
