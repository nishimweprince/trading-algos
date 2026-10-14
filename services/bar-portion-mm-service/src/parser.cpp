#include "bpmm/parser.hpp"

#include <charconv>
#include <string_view>

namespace bpmm {
namespace {

std::string_view value_after(std::string_view text, std::string_view key, std::size_t start = 0) noexcept {
  const auto pos = text.find(key, start);
  if (pos == std::string_view::npos) return {};
  auto value = text.substr(pos + key.size());
  while (!value.empty() && (value.front() == ' ' || value.front() == ':')) value.remove_prefix(1);
  return value;
}

bool parse_u64(std::string_view text, std::string_view key, std::uint64_t& out) noexcept {
  auto value = value_after(text, key);
  if (value.empty()) return false;
  const auto [ptr, ec] = std::from_chars(value.data(), value.data() + value.size(), out);
  return ec == std::errc{} && ptr != value.data();
}

bool parse_bool(std::string_view text, std::string_view key, bool& out) noexcept {
  auto value = value_after(text, key);
  if (value.starts_with("true")) { out = true; return true; }
  if (value.starts_with("false")) { out = false; return true; }
  return false;
}

bool parse_decimal(std::string_view text, std::string_view key, Decimal& out) noexcept {
  auto value = value_after(text, key);
  if (value.empty()) return false;
  if (value.front() == '"') value.remove_prefix(1);
  const auto end = value.find_first_of("\"},]");
  if (end == std::string_view::npos) return false;
  try {
    out = Decimal::parse(value.substr(0, end));
    return true;
  } catch (...) {
    return false;
  }
}

template <std::size_t N>
bool parse_levels(std::string_view text, std::string_view key, std::array<Level, N>& output,
                  std::uint16_t& count) noexcept {
  auto value = value_after(text, key);
  if (value.empty()) return false;
  const auto start = value.find('[');
  if (start == std::string_view::npos) return false;
  value.remove_prefix(start + 1);
  count = 0;
  while (!value.empty() && value.front() != ']') {
    const auto price_start = value.find('"');
    if (price_start == std::string_view::npos) return false;
    value.remove_prefix(price_start + 1);
    const auto price_end = value.find('"');
    if (price_end == std::string_view::npos) return false;
    const auto price = value.substr(0, price_end);
    value.remove_prefix(price_end + 1);
    const auto qty_start = value.find('"');
    if (qty_start == std::string_view::npos) return false;
    value.remove_prefix(qty_start + 1);
    const auto qty_end = value.find('"');
    if (qty_end == std::string_view::npos) return false;
    const auto quantity = value.substr(0, qty_end);
    value.remove_prefix(qty_end + 1);
    if (count >= N) return false;
    try {
      output[count++] = {Decimal::parse(price), Decimal::parse(quantity)};
    } catch (...) {
      return false;
    }
    const auto close = value.find(']');
    if (close == std::string_view::npos) return false;
    value.remove_prefix(close + 1);
    while (!value.empty() && (value.front() == ',' || value.front() == ' ')) value.remove_prefix(1);
  }
  return true;
}

}  // namespace

bool parse_stream_event(std::string_view json, std::uint64_t receive_ns,
                        ParsedEvent& output) noexcept {
  output = {};
  if (json.find("depthUpdate") != std::string_view::npos) {
    auto& event = output.depth;
    if (!parse_u64(json, "\"E\"", event.event_ms) ||
        !parse_u64(json, "\"T\"", event.transaction_ms) ||
        !parse_u64(json, "\"U\"", event.first_id) ||
        !parse_u64(json, "\"u\"", event.final_id) ||
        !parse_u64(json, "\"pu\"", event.previous_final_id) ||
        !parse_levels(json, "\"b\"", event.bids, event.bid_count) ||
        !parse_levels(json, "\"a\"", event.asks, event.ask_count)) return false;
    event.receive_ns = receive_ns;
    output.kind = ParsedKind::depth;
    return true;
  }
  if (json.find("aggTrade") != std::string_view::npos) {
    auto& event = output.trade;
    if (!parse_u64(json, "\"a\"", event.trade_id) ||
        !parse_u64(json, "\"T\"", event.trade_ms) ||
        !parse_decimal(json, "\"p\"", event.price) ||
        !parse_decimal(json, "\"q\"", event.quantity) ||
        !parse_bool(json, "\"m\"", event.buyer_is_maker)) return false;
    event.receive_ns = receive_ns;
    output.kind = ParsedKind::trade;
    return true;
  }
  if (json.find("kline") != std::string_view::npos) {
    const auto kline = json.find("\"k\"");
    if (kline == std::string_view::npos) return false;
    const auto section = json.substr(kline);
    auto& event = output.candle;
    if (!parse_u64(section, "\"t\"", event.open_ms) ||
        !parse_u64(section, "\"T\"", event.close_ms) ||
        !parse_decimal(section, "\"o\"", event.open) ||
        !parse_decimal(section, "\"h\"", event.high) ||
        !parse_decimal(section, "\"l\"", event.low) ||
        !parse_decimal(section, "\"c\"", event.close) ||
        !parse_bool(section, "\"x\"", event.closed)) return false;
    output.kind = ParsedKind::candle;
    return true;
  }
  if (json.find("markPriceUpdate") != std::string_view::npos) {
    auto& event = output.mark_price;
    Decimal rate;
    if (!parse_u64(json, "\"E\"", event.event_ms) ||
        !parse_u64(json, "\"T\"", event.next_funding_ms) ||
        !parse_decimal(json, "\"p\"", event.mark) ||
        !parse_decimal(json, "\"r\"", rate)) return false;
    event.receive_ns = receive_ns;
    event.funding_rate_ppm = static_cast<std::int64_t>(
        (static_cast<__int128>(rate.raw) * 1'000'000) / kScale);
    output.kind = ParsedKind::mark_price;
    return true;
  }
  return false;
}

bool parse_snapshot(std::string_view json, Snapshot& output) noexcept {
  output = {};
  return parse_u64(json, "\"lastUpdateId\"", output.last_update_id) &&
         parse_levels(json, "\"bids\"", output.bids, output.bid_count) &&
         parse_levels(json, "\"asks\"", output.asks, output.ask_count);
}

}  // namespace bpmm
