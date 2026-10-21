#pragma once

#include <array>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string_view>

namespace hla {

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

// Slot 0 is Hyperliquid (the quoted, slower venue); slot 1 is MT5 (fair value and hedge).
enum class Venue : std::uint8_t { hyperliquid = 0, mt5 = 1 };
constexpr std::size_t kVenues = 2;
constexpr std::size_t kMaxSymbols = 32;

inline constexpr std::size_t index(Venue venue) noexcept { return static_cast<std::size_t>(venue); }
inline constexpr std::string_view venue_name(Venue venue) noexcept {
  switch (venue) {
    case Venue::hyperliquid: return "hyperliquid";
    case Venue::mt5: return "mt5";
  }
  return "unknown";
}

struct Level final {
  Price price{};
  Quantity quantity{};
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

}  // namespace hla
