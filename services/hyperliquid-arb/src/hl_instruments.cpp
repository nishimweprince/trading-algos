#include "hla/hl_instruments.hpp"

#include "hla/json.hpp"

#include <algorithm>
#include <map>
#include <stdexcept>

namespace hla {
namespace {

std::string_view trim(std::string_view text) {
  while (!text.empty() && (text.front() == ' ' || text.front() == '\t')) text.remove_prefix(1);
  while (!text.empty() && (text.back() == ' ' || text.back() == '\t' || text.back() == '\n')) text.remove_suffix(1);
  return text;
}

std::string dex_body(std::string_view type, std::string_view dex) {
  auto body = "{\"type\":\"" + std::string(type) + "\"";
  if (!dex.empty()) body += ",\"dex\":\"" + std::string(dex) + "\"";
  return body + "}";
}

}  // namespace

std::string_view coin_dex(std::string_view coin) noexcept {
  const auto colon = coin.find(':');
  return colon == std::string_view::npos ? std::string_view{} : coin.substr(0, colon);
}

std::vector<std::string> parse_coins(std::string_view text) {
  std::vector<std::string> coins;
  while (!text.empty()) {
    const auto comma = text.find(',');
    const auto item = trim(text.substr(0, comma));
    text = comma == std::string_view::npos ? std::string_view{} : text.substr(comma + 1);
    if (item.empty()) continue;
    if (std::find(coins.begin(), coins.end(), item) != coins.end())
      throw std::invalid_argument("duplicate coin " + std::string(item));
    coins.emplace_back(item);
  }
  if (coins.empty()) throw std::invalid_argument("coin list is empty");
  if (coins.size() > kMaxSymbols) throw std::invalid_argument("coin list exceeds kMaxSymbols");
  return coins;
}

bool parse_dex_index(std::string_view perp_dexs, std::string_view dex, std::uint32_t& index, std::string& error) {
  if (dex.empty()) { index = 0; return true; }
  const auto pos = perp_dexs.find("\"name\":\"" + std::string(dex) + "\"");
  if (pos == std::string_view::npos) { error = "hyperliquid: unknown perp dex " + std::string(dex); return false; }
  // Entries are `null` or one object each; nested arrays (OI caps) sit inside the objects.
  std::uint32_t position = 0;
  int depth = 0;
  bool in_string = false;
  for (std::size_t i = perp_dexs.find('[') + 1; i < pos; ++i) {
    const char c = perp_dexs[i];
    if (in_string) {
      if (c == '\\') ++i;
      else if (c == '"') in_string = false;
      continue;
    }
    if (c == '"') in_string = true;
    else if (c == '{' || c == '[') ++depth;
    else if (c == '}' || c == ']') --depth;
    else if (c == ',' && depth == 0) ++position;
  }
  index = position;
  return true;
}

Price hl_tick(Price mid, int size_decimals) noexcept {
  if (mid.raw <= 0) return {};
  // Power of ten (in raw units) of the mid's leading digit.
  std::int64_t leading = 1;
  while (leading <= mid.raw / 10) leading *= 10;
  const auto five_figures = std::max<std::int64_t>(leading / 10'000, 1);
  std::int64_t max_decimals = kScale;  // 10^-(6 - szDecimals) in raw units
  for (int i = 0; i < 6 - size_decimals; ++i) max_decimals /= 10;
  return Price{std::max(five_figures, max_decimals)};
}

bool parse_hl_market(std::string_view meta, std::string_view mids, HlMarket& market, std::string& error) {
  const auto universe = meta.find("\"universe\"");
  const auto needle = "\"name\":\"" + market.coin + "\"";
  const auto pos = meta.find(needle, universe == std::string_view::npos ? 0 : universe);
  if (universe == std::string_view::npos || pos == std::string_view::npos) {
    error = "hyperliquid: unknown coin " + market.coin;
    return false;
  }
  // Universe entries are flat objects with exactly one "name" each.
  std::uint32_t meta_index = 0;
  for (auto at = meta.find("\"name\":\"", universe); at < pos; at = meta.find("\"name\":\"", at + 1)) ++meta_index;
  const auto entry = json::enclosing_object(meta, pos);
  bool delisted = false;
  if (json::boolean(entry, "\"isDelisted\"", delisted) && delisted) {
    error = "hyperliquid: " + market.coin + " is delisted";
    return false;
  }
  std::int64_t size_decimals = -1;
  std::int64_t max_leverage = 0;
  if (!json::i64(entry, "\"szDecimals\"", size_decimals) || size_decimals < 0 || size_decimals > 6 ||
      !json::i64(entry, "\"maxLeverage\"", max_leverage) || max_leverage <= 0 ||
      !json::decimal(mids, "\"" + market.coin + "\"", market.mid) || market.mid.raw <= 0) {
    error = "hyperliquid: incomplete meta or mid for " + market.coin;
    return false;
  }
  market.meta_index = meta_index;
  market.asset_id = asset_id(market.dex_index, meta_index);
  market.size_decimals = static_cast<int>(size_decimals);
  market.max_leverage = static_cast<int>(max_leverage);
  market.growth_mode = json::scalar(entry, "\"growthMode\"") == "enabled";
  market.only_isolated = false;
  json::boolean(entry, "\"onlyIsolated\"", market.only_isolated);
  std::int64_t step = kScale;
  for (std::int64_t i = 0; i < size_decimals; ++i) step /= 10;
  market.step = Quantity{step};
  market.tick = hl_tick(market.mid, market.size_decimals);
  return true;
}

bool load_markets(HttpClient& http, const std::vector<std::string>& coins, std::vector<HlMarket>& markets,
                  std::string& error) {
  const auto dexs = hyperliquid_info(http, "{\"type\":\"perpDexs\"}");
  if (!dexs.ok) { error = "hyperliquid perpDexs: " + dexs.error; return false; }
  struct DexData { std::string meta; std::string mids; };
  std::map<std::string, DexData, std::less<>> cache;
  markets.clear();
  for (const auto& coin : coins) {
    HlMarket market;
    market.coin = coin;
    market.dex = std::string(coin_dex(coin));
    if (!parse_dex_index(dexs.body, market.dex, market.dex_index, error)) return false;
    auto found = cache.find(market.dex);
    if (found == cache.end()) {
      const auto meta = hyperliquid_info(http, dex_body("meta", market.dex));
      if (!meta.ok) { error = "hyperliquid meta " + market.dex + ": " + meta.error; return false; }
      const auto mids = hyperliquid_info(http, dex_body("allMids", market.dex));
      if (!mids.ok) { error = "hyperliquid allMids " + market.dex + ": " + mids.error; return false; }
      found = cache.emplace(market.dex, DexData{meta.body, mids.body}).first;
    }
    if (!parse_hl_market(found->second.meta, found->second.mids, market, error)) return false;
    markets.push_back(std::move(market));
  }
  return true;
}

}  // namespace hla
