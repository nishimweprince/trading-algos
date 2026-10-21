#include "cxa/parser.hpp"

#include "cxa/json.hpp"

#include <memory>

namespace cxa {
namespace {

// json::levels writes into a fixed array; snapshots can be larger than a DepthEvent.
constexpr std::size_t kSnapshotLevels = 1000;

template <std::size_t N>
bool depth_levels(std::string_view text, std::string_view key, std::array<Level, N>& output,
                  std::uint16_t& count, bool& overflow) noexcept {
  if (json::levels(text, key, output, count)) return true;
  // Distinguish overflow from malformed input: count reached N while input remains.
  overflow = count >= N;
  return false;
}

}  // namespace

int symbol_index(std::span<const std::string> symbols, std::string_view name) noexcept {
  for (std::size_t i = 0; i < symbols.size(); ++i)
    if (symbols[i] == name) return static_cast<int>(i);
  return -1;
}

bool parse_binance_frame(std::string_view json, std::uint64_t receive_ns,
                         std::span<const std::string> symbols, ParsedFrame& output) noexcept {
  output.kind = FrameKind::ignored;
  const auto data = json.find("\"data\"");
  const auto body = data == std::string_view::npos ? json : json.substr(data);
  const auto symbol = symbol_index(symbols, json::scalar(body, "\"s\""));
  if (body.find("\"depthUpdate\"") != std::string_view::npos) {
    if (symbol < 0) return true;
    auto& event = output.depth;
    event.venue = Venue::binance;
    event.symbol = static_cast<std::uint16_t>(symbol);
    event.snapshot = false;
    event.receive_ns = receive_ns;
    bool overflow = false;
    if (!json::u64(body, "\"E\"", event.event_ms) || !json::u64(body, "\"U\"", event.first_id) ||
        !json::u64(body, "\"u\"", event.final_id) || !json::u64(body, "\"pu\"", event.previous_final_id)) {
      output.kind = FrameKind::error;
      return false;
    }
    if (!depth_levels(body, "\"b\"", event.bids, event.bid_count, overflow) ||
        !depth_levels(body, "\"a\"", event.asks, event.ask_count, overflow)) {
      output.kind = overflow ? FrameKind::overflow : FrameKind::error;
      return overflow;
    }
    output.kind = FrameKind::depth;
    return true;
  }
  if (body.find("\"markPriceUpdate\"") != std::string_view::npos) {
    if (symbol < 0) return true;
    auto& event = output.funding;
    event = {};
    event.venue = Venue::binance;
    event.symbol = static_cast<std::uint16_t>(symbol);
    event.receive_ns = receive_ns;
    if (!json::u64(body, "\"E\"", event.event_ms) || !json::u64(body, "\"T\"", event.next_funding_ms) ||
        !json::decimal(body, "\"p\"", event.mark) || !json::decimal(body, "\"r\"", event.funding_rate)) {
      output.kind = FrameKind::error;
      return false;
    }
    event.has_rate = true;
    output.kind = FrameKind::funding;
    return true;
  }
  return true;
}

bool parse_bybit_frame(std::string_view json, std::uint64_t receive_ns,
                       std::span<const std::string> symbols, ParsedFrame& output) noexcept {
  output.kind = FrameKind::ignored;
  const auto topic = json::scalar(json, "\"topic\"");
  if (topic.empty()) {
    // Control frames: pong and subscription acknowledgements.
    bool success = true;
    if (json::boolean(json, "\"success\"", success) && !success) {
      output.kind = FrameKind::error;
      return false;
    }
    return true;
  }
  const auto data = json.find("\"data\"");
  if (data == std::string_view::npos) {
    output.kind = FrameKind::error;
    return false;
  }
  const auto body = json.substr(data);
  if (topic.starts_with("orderbook.")) {
    const auto dot = topic.rfind('.');
    const auto symbol = symbol_index(symbols, topic.substr(dot + 1));
    if (symbol < 0) return true;
    auto& event = output.depth;
    event.venue = Venue::bybit;
    event.symbol = static_cast<std::uint16_t>(symbol);
    event.receive_ns = receive_ns;
    event.snapshot = json::scalar(json, "\"type\"") == "snapshot";
    event.previous_final_id = 0;
    bool overflow = false;
    if (!json::u64(json, "\"ts\"", event.event_ms) || !json::u64(body, "\"u\"", event.final_id)) {
      output.kind = FrameKind::error;
      return false;
    }
    event.first_id = event.final_id;
    // u == 1 is a service-restart snapshot even when typed as delta.
    if (event.final_id == 1) event.snapshot = true;
    if (!depth_levels(body, "\"b\"", event.bids, event.bid_count, overflow) ||
        !depth_levels(body, "\"a\"", event.asks, event.ask_count, overflow)) {
      output.kind = overflow ? FrameKind::overflow : FrameKind::error;
      return overflow;
    }
    output.kind = FrameKind::depth;
    return true;
  }
  if (topic.starts_with("tickers.")) {
    const auto symbol = symbol_index(symbols, topic.substr(8));
    if (symbol < 0) return true;
    auto& event = output.funding;
    event = {};
    event.venue = Venue::bybit;
    event.symbol = static_cast<std::uint16_t>(symbol);
    event.receive_ns = receive_ns;
    if (!json::u64(json, "\"ts\"", event.event_ms)) {
      output.kind = FrameKind::error;
      return false;
    }
    // Deltas carry only the fields that changed.
    json::decimal(body, "\"markPrice\"", event.mark);
    json::u64(body, "\"nextFundingTime\"", event.next_funding_ms);
    event.has_rate = json::decimal(body, "\"fundingRate\"", event.funding_rate);
    output.kind = FrameKind::funding;
    return true;
  }
  return true;
}

namespace {

// {"px":"2455.9","sz":"58.1","n":11} or null.
bool hyperliquid_level(std::string_view side, std::array<Level, kMaxDepthChanges>& levels,
                       std::uint16_t& count) noexcept {
  count = 0;
  if (side.starts_with("null")) return true;
  Level level;
  if (!json::decimal(side, "\"px\"", level.price) || !json::decimal(side, "\"sz\"", level.quantity)) return false;
  levels[0] = level;
  count = 1;
  return true;
}

}  // namespace

bool parse_hyperliquid_frame(std::string_view json, std::uint64_t receive_ns,
                             std::span<const std::string> symbols, ParsedFrame& output) noexcept {
  output.kind = FrameKind::ignored;
  const auto channel = json::scalar(json, "\"channel\"");
  if (channel == "error") {
    output.kind = FrameKind::error;
    return false;
  }
  if (channel != "bbo" && channel != "activeAssetCtx") return true;  // subscriptionResponse, pong
  const auto data = json.find("\"data\"");
  if (data == std::string_view::npos) {
    output.kind = FrameKind::error;
    return false;
  }
  const auto body = json.substr(data);
  const auto symbol = symbol_index(symbols, json::scalar(body, "\"coin\""));
  if (symbol < 0) return true;
  if (channel == "bbo") {
    auto& event = output.depth;
    event.venue = Venue::hyperliquid;
    event.symbol = static_cast<std::uint16_t>(symbol);
    event.receive_ns = receive_ns;
    event.snapshot = true;
    event.previous_final_id = 0;
    auto sides = json::value_after(body, "\"bbo\"");
    if (!json::u64(body, "\"time\"", event.event_ms) || !sides.starts_with('[')) {
      output.kind = FrameKind::error;
      return false;
    }
    event.first_id = event.final_id = event.event_ms;
    sides.remove_prefix(1);
    const auto split = sides.starts_with("null") ? sides.find(',') : sides.find("},");
    if (split == std::string_view::npos ||
        !hyperliquid_level(sides, event.bids, event.bid_count)) {
      output.kind = FrameKind::error;
      return false;
    }
    auto ask = sides.substr(split + (sides.starts_with("null") ? 1 : 2));
    while (!ask.empty() && ask.front() == ' ') ask.remove_prefix(1);
    if (!hyperliquid_level(ask, event.asks, event.ask_count)) {
      output.kind = FrameKind::error;
      return false;
    }
    output.kind = FrameKind::depth;
    return true;
  }
  auto& event = output.funding;
  event = {};
  event.venue = Venue::hyperliquid;
  event.symbol = static_cast<std::uint16_t>(symbol);
  event.receive_ns = receive_ns;
  event.event_ms = receive_ns / 1'000'000;
  constexpr std::uint64_t kHourMs = 3'600'000;
  event.next_funding_ms = (event.event_ms / kHourMs + 1) * kHourMs;
  json::decimal(body, "\"markPx\"", event.mark);
  event.has_rate = json::decimal(body, "\"funding\"", event.funding_rate);
  if (!event.has_rate) {
    output.kind = FrameKind::error;
    return false;
  }
  output.kind = FrameKind::funding;
  return true;
}

bool parse_binance_snapshot(std::string_view json, BookSnapshot& output) {
  auto levels = std::make_unique<std::array<Level, kSnapshotLevels>>();
  std::uint16_t count = 0;
  if (!json::u64(json, "\"lastUpdateId\"", output.last_update_id)) return false;
  if (!json::levels(json, "\"bids\"", *levels, count)) return false;
  output.bids.assign(levels->begin(), levels->begin() + count);
  if (!json::levels(json, "\"asks\"", *levels, count)) return false;
  output.asks.assign(levels->begin(), levels->begin() + count);
  return !output.bids.empty() && !output.asks.empty();
}

}  // namespace cxa
