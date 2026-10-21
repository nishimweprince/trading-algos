#pragma once

#include "hla/net.hpp"
#include "hla/types.hpp"

#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

// Read-only Hyperliquid `info` queries used at startup and by the aux thread. HIP-3 dexes
// keep separate clearinghouses and order lists, so those queries take a dex ("" = validator).
namespace hla {

bool valid_address(std::string_view address) noexcept;
std::string lowercase(std::string_view text);

struct HlPosition final {
  std::string coin;
  Decimal size{};  // signed szi
};

struct HlAccountState final {
  Decimal account_value{};
  Decimal withdrawable{};
  std::vector<HlPosition> positions;
};

struct HlOpenOrder final {
  std::string coin;
  std::uint64_t oid{};
};

struct HlRateLimit final {
  Decimal cum_volume{};
  std::uint64_t used{};
  std::uint64_t cap{};
  [[nodiscard]] std::int64_t remaining() const noexcept {
    return static_cast<std::int64_t>(cap) - static_cast<std::int64_t>(used);
  }
};

bool parse_clearinghouse(std::string_view body, HlAccountState& out);
bool parse_open_orders(std::string_view body, std::vector<HlOpenOrder>& out);
bool parse_rate_limit(std::string_view body, HlRateLimit& out);
// userFees: userCrossRate (taker) and userAddRate (maker), in ppm.
bool parse_user_fees(std::string_view body, std::int64_t& taker_ppm, std::int64_t& maker_ppm);
// extraAgents: approved API wallet addresses, lowercased.
bool parse_agents(std::string_view body, std::vector<std::string>& addresses);

bool fetch_clearinghouse(HttpClient& http, std::string_view user, std::string_view dex, HlAccountState& out,
                         std::string& error);
bool fetch_open_orders(HttpClient& http, std::string_view user, std::string_view dex, std::vector<HlOpenOrder>& out,
                       std::string& error);
bool fetch_rate_limit(HttpClient& http, std::string_view user, HlRateLimit& out, std::string& error);
bool fetch_user_fees(HttpClient& http, std::string_view user, std::int64_t& taker_ppm, std::int64_t& maker_ppm,
                     std::string& error);
bool fetch_agents(HttpClient& http, std::string_view user, std::vector<std::string>& addresses, std::string& error);

}  // namespace hla
