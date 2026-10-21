#include "hla/hl_parser.hpp"

#include "hla/json.hpp"

namespace hla {
namespace {

// {"px":"2455.9","sz":"58.1","n":11} or null.
bool hl_level(std::string_view side, Level& level, bool& present) noexcept {
  present = false;
  if (side.starts_with("null")) return true;
  if (!json::decimal(side, "\"px\"", level.price) || !json::decimal(side, "\"sz\"", level.quantity)) return false;
  present = true;
  return true;
}

}  // namespace

int symbol_index(std::span<const std::string> symbols, std::string_view name) noexcept {
  for (std::size_t i = 0; i < symbols.size(); ++i)
    if (symbols[i] == name) return static_cast<int>(i);
  return -1;
}

bool parse_hl_frame(std::string_view json, std::uint64_t receive_ns, std::span<const std::string> coins,
                    HlFrame& output) noexcept {
  output.kind = HlFrameKind::ignored;
  const auto channel = json::scalar(json, "\"channel\"");
  if (channel == "error") {
    output.kind = HlFrameKind::error;
    return false;
  }
  if (channel != "bbo" && channel != "activeAssetCtx") return true;  // subscriptionResponse, pong, post
  const auto data = json.find("\"data\"");
  if (data == std::string_view::npos) {
    output.kind = HlFrameKind::error;
    return false;
  }
  const auto body = json.substr(data);
  const auto symbol = symbol_index(coins, json::scalar(body, "\"coin\""));
  if (symbol < 0) return true;
  if (channel == "bbo") {
    auto& event = output.bbo;
    event = {};
    event.symbol = static_cast<std::uint16_t>(symbol);
    event.receive_ns = receive_ns;
    auto sides = json::value_after(body, "\"bbo\"");
    if (!json::u64(body, "\"time\"", event.event_ms) || !sides.starts_with('[')) {
      output.kind = HlFrameKind::error;
      return false;
    }
    sides.remove_prefix(1);
    const bool null_bid = sides.starts_with("null");
    const auto split = null_bid ? sides.find(',') : sides.find("},");
    if (split == std::string_view::npos || !hl_level(sides, event.bid, event.has_bid)) {
      output.kind = HlFrameKind::error;
      return false;
    }
    auto ask = sides.substr(split + (null_bid ? 1 : 2));
    while (!ask.empty() && ask.front() == ' ') ask.remove_prefix(1);
    if (!hl_level(ask, event.ask, event.has_ask)) {
      output.kind = HlFrameKind::error;
      return false;
    }
    output.kind = HlFrameKind::bbo;
    return true;
  }
  auto& event = output.ctx;
  event = {};
  event.symbol = static_cast<std::uint16_t>(symbol);
  event.receive_ns = receive_ns;
  if (!json::decimal(body, "\"funding\"", event.funding) || !json::decimal(body, "\"markPx\"", event.mark) ||
      !json::decimal(body, "\"oraclePx\"", event.oracle)) {
    output.kind = HlFrameKind::error;
    return false;
  }
  output.kind = HlFrameKind::ctx;
  return true;
}

}  // namespace hla
