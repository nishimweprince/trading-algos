#include "cxa/instruments.hpp"

#include "cxa/json.hpp"

#include <algorithm>
#include <numeric>
#include <stdexcept>

namespace cxa {
namespace {

std::string_view trim(std::string_view text) {
  while (!text.empty() && (text.front() == ' ' || text.front() == '\t')) text.remove_prefix(1);
  while (!text.empty() && (text.back() == ' ' || text.back() == '\t' || text.back() == '\n')) text.remove_suffix(1);
  return text;
}

// Text of the symbol object that starts at "\"symbol\":\"<name>\"" and ends before the
// next symbol object.
std::string_view symbol_section(std::string_view text, std::string_view name) {
  const auto needle = std::string("\"symbol\":\"") + std::string(name) + "\"";
  const auto start = text.find(needle);
  if (start == std::string_view::npos) return {};
  const auto next = text.find("\"symbol\":\"", start + needle.size());
  return text.substr(start, next == std::string_view::npos ? std::string_view::npos : next - start);
}

std::string_view filter(std::string_view section, std::string_view type) {
  const auto needle = std::string("\"filterType\":\"") + std::string(type) + "\"";
  const auto pos = section.find(needle);
  if (pos == std::string_view::npos) return {};
  return json::enclosing_object(section, pos);
}

}  // namespace

std::string hyperliquid_coin(std::string_view binance_symbol) {
  auto coin = binance_symbol;
  if (coin.ends_with("USDT")) coin.remove_suffix(4);
  if (coin.starts_with("1000")) return "k" + std::string(coin.substr(4));
  return std::string(coin);
}

std::vector<Instrument> parse_basket(std::string_view text, Venue peer) {
  std::vector<Instrument> basket;
  while (!text.empty()) {
    const auto comma = text.find(',');
    const auto item = trim(text.substr(0, comma));
    text = comma == std::string_view::npos ? std::string_view{} : text.substr(comma + 1);
    if (item.empty()) continue;
    Instrument instrument;
    const auto first = item.find(':');
    if (first == std::string_view::npos) {
      instrument.name = std::string(item);
      instrument.symbol = {instrument.name,
                           peer == Venue::hyperliquid ? hyperliquid_coin(instrument.name) : instrument.name};
    } else {
      const auto second = item.find(':', first + 1);
      if (second == std::string_view::npos) throw std::invalid_argument("basket entry needs name:binance:peer");
      instrument.name = std::string(item.substr(0, first));
      instrument.symbol = {std::string(item.substr(first + 1, second - first - 1)),
                           std::string(item.substr(second + 1))};
    }
    for (const auto& existing : basket)
      if (existing.name == instrument.name) throw std::invalid_argument("duplicate basket entry " + instrument.name);
    basket.push_back(std::move(instrument));
  }
  if (basket.empty()) throw std::invalid_argument("basket is empty");
  if (basket.size() > kMaxSymbols) throw std::invalid_argument("basket exceeds kMaxSymbols");
  return basket;
}

bool parse_binance_instrument(std::string_view exchange_info, Instrument& instrument, std::string& error) {
  const auto& name = instrument.symbol[index(Venue::binance)];
  const auto section = symbol_section(exchange_info, name);
  if (section.empty()) { error = "binance: unknown symbol " + name; return false; }
  if (json::scalar(section, "\"contractType\"") != "PERPETUAL" || json::scalar(section, "\"status\"") != "TRADING" ||
      json::scalar(section, "\"quoteAsset\"") != "USDT") {
    error = "binance: " + name + " is not a trading USDT perpetual";
    return false;
  }
  const auto price = filter(section, "PRICE_FILTER");
  const auto lot = filter(section, "LOT_SIZE");
  const auto notional = filter(section, "MIN_NOTIONAL");
  const auto v = index(Venue::binance);
  if (!json::decimal(price, "\"tickSize\"", instrument.tick[v]) ||
      !json::decimal(lot, "\"stepSize\"", instrument.step[v]) ||
      !json::decimal(lot, "\"minQty\"", instrument.min_qty[v]) ||
      !json::decimal(notional, "\"notional\"", instrument.min_notional[v])) {
    error = "binance: incomplete filters for " + name;
    return false;
  }
  return true;
}

bool parse_bybit_instrument(std::string_view instruments_info, Instrument& instrument, std::string& error) {
  const auto& name = instrument.symbol[index(Venue::bybit)];
  const auto section = symbol_section(instruments_info, name);
  if (section.empty()) { error = "bybit: unknown symbol " + name; return false; }
  if (json::scalar(section, "\"contractType\"") != "LinearPerpetual" ||
      json::scalar(section, "\"status\"") != "Trading" || json::scalar(section, "\"quoteCoin\"") != "USDT") {
    error = "bybit: " + name + " is not a trading USDT linear perpetual";
    return false;
  }
  const auto v = index(Venue::bybit);
  if (!json::decimal(section, "\"tickSize\"", instrument.tick[v]) ||
      !json::decimal(section, "\"qtyStep\"", instrument.step[v]) ||
      !json::decimal(section, "\"minOrderQty\"", instrument.min_qty[v]) ||
      !json::decimal(section, "\"minNotionalValue\"", instrument.min_notional[v])) {
    error = "bybit: incomplete filters for " + name;
    return false;
  }
  return true;
}

Price hyperliquid_tick(Price mid, int size_decimals) noexcept {
  if (mid.raw <= 0) return {};
  // Power of ten (in raw units) of the mid's leading digit.
  std::int64_t leading = 1;
  while (leading <= mid.raw / 10) leading *= 10;
  const auto five_figures = std::max<std::int64_t>(leading / 10'000, 1);
  std::int64_t max_decimals = kScale;  // 10^-(6 - szDecimals) in raw units
  for (int i = 0; i < 6 - size_decimals; ++i) max_decimals /= 10;
  return Price{std::max(five_figures, max_decimals)};
}

bool parse_hyperliquid_instrument(std::string_view meta, std::string_view mids, Instrument& instrument,
                                  std::string& error) {
  const auto& name = instrument.symbol[index(Venue::hyperliquid)];
  const auto needle = "\"name\":\"" + name + "\"";
  const auto pos = meta.find(needle);
  if (pos == std::string_view::npos) { error = "hyperliquid: unknown coin " + name; return false; }
  const auto entry = json::enclosing_object(meta, pos);
  bool delisted = false;
  if (json::boolean(entry, "\"isDelisted\"", delisted) && delisted) {
    error = "hyperliquid: " + name + " is delisted";
    return false;
  }
  std::int64_t size_decimals = -1;
  Price mid;
  if (!json::i64(entry, "\"szDecimals\"", size_decimals) || size_decimals < 0 || size_decimals > 6 ||
      !json::decimal(mids, "\"" + name + "\"", mid) || mid.raw <= 0) {
    error = "hyperliquid: incomplete meta or mid for " + name;
    return false;
  }
  const auto v = index(Venue::hyperliquid);
  std::int64_t step = kScale;
  for (std::int64_t i = 0; i < size_decimals; ++i) step /= 10;
  instrument.step[v] = Quantity{step};
  instrument.min_qty[v] = Quantity{step};
  instrument.min_notional[v] = Decimal{10 * kScale};
  instrument.tick[v] = hyperliquid_tick(mid, static_cast<int>(size_decimals));
  return true;
}

bool finalize_instrument(Instrument& instrument, std::string& error) {
  const auto a = instrument.step[0].raw;
  const auto b = instrument.step[1].raw;
  if (a <= 0 || b <= 0) { error = instrument.name + ": non-positive lot step"; return false; }
  const auto common = std::lcm(a, b);
  if (common > 1'000'000 * kScale) { error = instrument.name + ": incompatible lot steps"; return false; }
  instrument.common_step = Quantity{common};
  const auto min_qty = std::max(instrument.min_qty[0].raw, instrument.min_qty[1].raw);
  instrument.common_min_qty = round_up(Quantity{min_qty}, instrument.common_step);
  return true;
}

}  // namespace cxa
