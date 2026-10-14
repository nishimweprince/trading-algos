#pragma once

#include "bpmm/types.hpp"

#include <array>
#include <cstdint>
#include <span>
#include <string_view>

namespace bpmm {

struct Snapshot final {
  std::uint64_t last_update_id{};
  std::array<Level, 1024> bids{};
  std::array<Level, 1024> asks{};
  std::uint16_t bid_count{};
  std::uint16_t ask_count{};
};

enum class ParsedKind : std::uint8_t { unknown, depth, trade, candle, mark_price };
struct ParsedEvent final {
  ParsedKind kind{ParsedKind::unknown};
  DepthEvent depth{};
  TradeEvent trade{};
  CandleEvent candle{};
  MarkPriceEvent mark_price{};
};

bool parse_stream_event(std::string_view json, std::uint64_t receive_ns,
                        ParsedEvent& output) noexcept;
bool parse_snapshot(std::string_view json, Snapshot& output) noexcept;

}  // namespace bpmm
