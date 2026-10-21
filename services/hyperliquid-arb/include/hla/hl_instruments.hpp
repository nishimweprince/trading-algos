#pragma once

#include "hla/net.hpp"
#include "hla/types.hpp"

#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace hla {

// One Hyperliquid perp: a validator perp ("BTC") or a HIP-3 market ("xyz:JPY").
struct HlMarket final {
  std::string coin;
  std::string dex;               // "" for validator perps, else the HIP-3 deployer prefix
  std::uint32_t dex_index{};     // position in `perpDexs`; 0 is the validator dex
  std::uint32_t meta_index{};    // position in that dex's `meta` universe
  std::uint32_t asset_id{};      // the `a` field in order actions
  int size_decimals{};
  int max_leverage{};
  bool growth_mode{};            // HIP-3 fee discount; gold, for one, is not in growth mode
  bool only_isolated{};
  Price mid{};
  Price tick{};
  Quantity step{};
};

// "xyz:JPY" -> "xyz"; "BTC" -> "".
std::string_view coin_dex(std::string_view coin) noexcept;

// "xyz:JPY, xyz:EUR,BTC" -> coins in order. Throws on duplicates, empty, or > kMaxSymbols.
std::vector<std::string> parse_coins(std::string_view text);

// Index of `dex` in the `perpDexs` response ([null, {"name":"xyz",...}, ...]).
bool parse_dex_index(std::string_view perp_dexs, std::string_view dex, std::uint32_t& index, std::string& error);

// Validator perps use the universe index; HIP-3 perps use 100000 + dex_index * 10000 + index.
constexpr std::uint32_t asset_id(std::uint32_t dex_index, std::uint32_t meta_index) noexcept {
  return dex_index == 0 ? meta_index : 100'000 + dex_index * 10'000 + meta_index;
}

// Prices allow 5 significant figures and at most 6 - szDecimals decimals, so the tick
// is derived from the current mid.
Price hl_tick(Price mid, int size_decimals) noexcept;

// Fills `market` (coin, dex and dex_index already set) from that dex's `meta` and `allMids`.
bool parse_hl_market(std::string_view meta, std::string_view mids, HlMarket& market, std::string& error);

// Fetches `perpDexs`, then `meta` + `allMids` once per dex in the basket.
bool load_markets(HttpClient& http, const std::vector<std::string>& coins, std::vector<HlMarket>& markets,
                  std::string& error);

}  // namespace hla
