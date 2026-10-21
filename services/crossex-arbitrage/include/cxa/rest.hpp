#pragma once

#include "cxa/instruments.hpp"
#include "cxa/types.hpp"

#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace cxa {

std::uint64_t wall_ns() noexcept;
std::string hmac_sha256_hex(std::string_view secret, std::string_view payload);

struct HttpResult final {
  bool ok{};
  long status{};
  std::string body;
  std::uint64_t elapsed_ns{};
  std::string error;
};

// One persistent curl handle per thread: connections are kept alive between calls so
// RTT samples measure request latency rather than TLS setup.
class HttpClient final {
 public:
  HttpClient();
  ~HttpClient();
  HttpClient(const HttpClient&) = delete;
  HttpClient& operator=(const HttpClient&) = delete;

  HttpResult get(const std::string& url, const std::vector<std::string>& headers = {});
  HttpResult post(const std::string& url, const std::string& body, const std::vector<std::string>& headers = {});

 private:
  HttpResult perform(const std::string& url, const std::string* body, const std::vector<std::string>& headers);
  void* curl_{};
};

struct Credentials final {
  std::string key;
  std::string secret;
  [[nodiscard]] bool present() const noexcept { return !key.empty() && !secret.empty(); }
};

// CXA_BINANCE_API_KEY / CXA_BINANCE_API_SECRET, CXA_BYBIT_API_KEY / CXA_BYBIT_API_SECRET.
// Hyperliquid needs no key in shadow: `key` holds the public CXA_HL_ADDRESS and the
// secret stays empty (info requests are unsigned).
Credentials load_credentials(Venue venue);

HttpResult signed_get(HttpClient& http, Venue venue, const Credentials& credentials, std::string_view base,
                      std::string_view path, std::string_view query);

// Refuses any key that can trade, transfer, or withdraw. Shadow mode must run with
// read-only keys only. Hyperliquid passes with a well-formed address and no key.
bool verify_read_only(HttpClient& http, Venue venue, const Credentials& credentials, std::string& detail);

// Account taker and maker fees per basket symbol, in ppm.
bool fetch_fees(HttpClient& http, Venue venue, const Credentials& credentials,
                const std::vector<Instrument>& basket, std::vector<std::int64_t>& taker_ppm,
                std::vector<std::int64_t>& maker_ppm, std::string& error);

bool load_instruments(HttpClient& http, std::vector<Instrument>& basket, std::string& error,
                      Venue peer = Venue::bybit);

// Hyperliquid userFees response: userCrossRate (taker) and userAddRate (maker), in ppm.
bool parse_hyperliquid_fees(std::string_view body, std::int64_t& taker_ppm, std::int64_t& maker_ppm);
bool valid_hyperliquid_address(std::string_view address) noexcept;

// Signed, read-only request on the order gateway host: RTT proxy for order entry.
// Hyperliquid: an unsigned exchangeStatus request to the host that serves /exchange.
HttpResult signed_probe(HttpClient& http, Venue venue, const Credentials& credentials);
// Unsigned server-time request; `server_ms` is the venue clock.
HttpResult time_probe(HttpClient& http, Venue venue, std::uint64_t& server_ms);

}  // namespace cxa
