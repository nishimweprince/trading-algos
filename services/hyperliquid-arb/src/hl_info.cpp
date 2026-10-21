#include "hla/hl_info.hpp"

#include "hla/json.hpp"

#include <algorithm>
#include <cctype>

namespace hla {
namespace {

std::string user_body(std::string_view type, std::string_view user, std::string_view dex = {}) {
  auto body = "{\"type\":\"" + std::string(type) + "\",\"user\":\"" + std::string(user) + "\"";
  if (!dex.empty()) body += ",\"dex\":\"" + std::string(dex) + "\"";
  return body + "}";
}

bool query(HttpClient& http, const std::string& body, std::string& response, std::string& error) {
  const auto result = hyperliquid_info(http, body);
  if (!result.ok) {
    error = "hyperliquid info " + body + ": " + result.error;
    return false;
  }
  response = result.body;
  return true;
}

}  // namespace

bool valid_address(std::string_view address) noexcept {
  if (address.size() != 42 || !address.starts_with("0x")) return false;
  return std::all_of(address.begin() + 2, address.end(),
                     [](char c) { return std::isxdigit(static_cast<unsigned char>(c)) != 0; });
}

std::string lowercase(std::string_view text) {
  std::string out(text);
  std::transform(out.begin(), out.end(), out.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  return out;
}

bool parse_clearinghouse(std::string_view body, HlAccountState& out) {
  out = {};
  if (!json::decimal(body, "\"accountValue\"", out.account_value) ||
      !json::decimal(body, "\"withdrawable\"", out.withdrawable))
    return false;
  for (const auto item : json::array_items(json::value_after(body, "\"assetPositions\""))) {
    HlPosition position;
    position.coin = std::string(json::scalar(item, "\"coin\""));
    if (position.coin.empty() || !json::decimal(item, "\"szi\"", position.size)) return false;
    out.positions.push_back(std::move(position));
  }
  return true;
}

bool parse_open_orders(std::string_view body, std::vector<HlOpenOrder>& out) {
  out.clear();
  if (!body.starts_with('[')) return false;
  for (const auto item : json::array_items(body)) {
    HlOpenOrder order;
    order.coin = std::string(json::scalar(item, "\"coin\""));
    if (order.coin.empty() || !json::u64(item, "\"oid\"", order.oid)) return false;
    out.push_back(std::move(order));
  }
  return true;
}

bool parse_rate_limit(std::string_view body, HlRateLimit& out) {
  return json::decimal(body, "\"cumVlm\"", out.cum_volume) && json::u64(body, "\"nRequestsUsed\"", out.used) &&
         json::u64(body, "\"nRequestsCap\"", out.cap);
}

bool parse_user_fees(std::string_view body, std::int64_t& taker_ppm, std::int64_t& maker_ppm) {
  return json::rate_ppm(body, "\"userCrossRate\"", taker_ppm) && json::rate_ppm(body, "\"userAddRate\"", maker_ppm);
}

bool parse_agents(std::string_view body, std::vector<std::string>& addresses) {
  addresses.clear();
  if (!body.starts_with('[')) return false;
  for (const auto item : json::array_items(body)) {
    const auto address = json::scalar(item, "\"address\"");
    if (!valid_address(address)) return false;
    addresses.push_back(lowercase(address));
  }
  return true;
}

bool fetch_clearinghouse(HttpClient& http, std::string_view user, std::string_view dex, HlAccountState& out,
                         std::string& error) {
  std::string body;
  if (!query(http, user_body("clearinghouseState", user, dex), body, error)) return false;
  if (!parse_clearinghouse(body, out)) {
    error = "hyperliquid clearinghouseState " + std::string(dex) + ": unexpected response";
    return false;
  }
  return true;
}

bool fetch_open_orders(HttpClient& http, std::string_view user, std::string_view dex, std::vector<HlOpenOrder>& out,
                       std::string& error) {
  std::string body;
  if (!query(http, user_body("openOrders", user, dex), body, error)) return false;
  if (!parse_open_orders(body, out)) {
    error = "hyperliquid openOrders " + std::string(dex) + ": unexpected response";
    return false;
  }
  return true;
}

bool fetch_rate_limit(HttpClient& http, std::string_view user, HlRateLimit& out, std::string& error) {
  std::string body;
  if (!query(http, user_body("userRateLimit", user), body, error)) return false;
  if (!parse_rate_limit(body, out)) {
    error = "hyperliquid userRateLimit: unexpected response";
    return false;
  }
  return true;
}

bool fetch_user_fees(HttpClient& http, std::string_view user, std::int64_t& taker_ppm, std::int64_t& maker_ppm,
                     std::string& error) {
  std::string body;
  if (!query(http, user_body("userFees", user), body, error)) return false;
  if (!parse_user_fees(body, taker_ppm, maker_ppm)) {
    error = "hyperliquid userFees: no userCrossRate/userAddRate";
    return false;
  }
  return true;
}

bool fetch_agents(HttpClient& http, std::string_view user, std::vector<std::string>& addresses, std::string& error) {
  std::string body;
  if (!query(http, user_body("extraAgents", user), body, error)) return false;
  if (!parse_agents(body, addresses)) {
    error = "hyperliquid extraAgents: unexpected response";
    return false;
  }
  return true;
}

}  // namespace hla
