#pragma once

#include <array>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string_view>

namespace bpmm {

constexpr std::int64_t kScale = 100'000'000;

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

struct Level final {
  Price price{};
  Quantity quantity{};
};

constexpr std::size_t kMaxDepthChanges = 128;

struct DepthEvent final {
  std::uint64_t event_ms{};
  std::uint64_t transaction_ms{};
  std::uint64_t first_id{};
  std::uint64_t final_id{};
  std::uint64_t previous_final_id{};
  std::uint64_t receive_ns{};
  std::array<Level, kMaxDepthChanges> bids{};
  std::array<Level, kMaxDepthChanges> asks{};
  std::uint16_t bid_count{};
  std::uint16_t ask_count{};
};

struct TradeEvent final {
  std::uint64_t trade_id{};
  std::uint64_t trade_ms{};
  std::uint64_t receive_ns{};
  Price price{};
  Quantity quantity{};
  bool buyer_is_maker{};
};

struct CandleEvent final {
  std::uint64_t open_ms{};
  std::uint64_t close_ms{};
  Price open{};
  Price high{};
  Price low{};
  Price close{};
  bool closed{};
};

struct MarkPriceEvent final {
  std::uint64_t event_ms{};
  std::uint64_t next_funding_ms{};
  std::uint64_t receive_ns{};
  Price mark{};
  std::int64_t funding_rate_ppm{};
};

enum class BookState : std::uint8_t { empty, syncing, verified, gap };
enum class IntentKind : std::uint8_t { place, cancel, cancel_all, none };

struct QuoteIntent final {
  IntentKind kind{IntentKind::none};
  Side side{Side::buy};
  std::uint8_t level{};
  Price price{};
  Quantity quantity{};
  bool reduce_only{};
  std::uint64_t decision_ns{};
  std::uint64_t sequence{};
};

constexpr std::size_t kMaxIntents = 8;
struct IntentBatch final {
  std::array<QuoteIntent, kMaxIntents> values{};
  std::uint8_t size{};

  void push(const QuoteIntent& value) noexcept {
    if (size < values.size()) values[size++] = value;
  }
};

inline constexpr std::int64_t mul_scaled(std::int64_t lhs, std::int64_t rhs) noexcept {
  return static_cast<std::int64_t>((static_cast<__int128>(lhs) * rhs) / kScale);
}

inline constexpr Price round_down(Price value, Price tick) noexcept {
  return tick.raw <= 0 ? value : Price{(value.raw / tick.raw) * tick.raw};
}

inline constexpr Price round_up(Price value, Price tick) noexcept {
  if (tick.raw <= 0) return value;
  return Price{((value.raw + tick.raw - 1) / tick.raw) * tick.raw};
}

}  // namespace bpmm
