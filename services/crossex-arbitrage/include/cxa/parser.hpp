#pragma once

#include "cxa/types.hpp"

#include <cstdint>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace cxa {

enum class FrameKind : std::uint8_t { ignored, depth, funding, overflow, error };

struct ParsedFrame final {
  FrameKind kind{FrameKind::ignored};
  DepthEvent depth{};
  FundingEvent funding{};
};

// Binance REST depth snapshot; larger than a DepthEvent so it travels separately.
struct BookSnapshot final {
  Venue venue{Venue::binance};
  std::uint16_t symbol{};
  std::uint64_t receive_ns{};
  std::uint64_t last_update_id{};
  std::vector<Level> bids;
  std::vector<Level> asks;
};

// `symbols` are the venue-native names in basket order. Returns false only when a
// recognised book frame is malformed; `overflow` means the diff exceeded kMaxDepthChanges.
bool parse_binance_frame(std::string_view json, std::uint64_t receive_ns,
                         std::span<const std::string> symbols, ParsedFrame& output) noexcept;
bool parse_bybit_frame(std::string_view json, std::uint64_t receive_ns,
                       std::span<const std::string> symbols, ParsedFrame& output) noexcept;
bool parse_binance_snapshot(std::string_view json, BookSnapshot& output);

int symbol_index(std::span<const std::string> symbols, std::string_view name) noexcept;

}  // namespace cxa
