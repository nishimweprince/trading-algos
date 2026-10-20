#include "cxa/rest.hpp"

#include "cxa/json.hpp"

#include <curl/curl.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>

#include <chrono>
#include <cstdlib>

namespace cxa {
namespace {

constexpr std::string_view kBinanceFutures = "https://fapi.binance.com";
constexpr std::string_view kBinanceSpot = "https://api.binance.com";
constexpr std::string_view kBybit = "https://api.bybit.com";
constexpr std::string_view kRecvWindow = "5000";

std::size_t append(void* data, std::size_t size, std::size_t count, void* context) {
  static_cast<std::string*>(context)->append(static_cast<const char*>(data), size * count);
  return size * count;
}

std::string env(const char* name) {
  const auto* value = std::getenv(name);
  return value == nullptr ? std::string{} : std::string(value);
}

bool api_ok(Venue venue, const HttpResult& result, std::string& error) {
  if (!result.ok) {
    error = std::string(venue_name(venue)) + ": " + result.error;
    return false;
  }
  if (venue == Venue::bybit) {
    std::int64_t code = -1;
    if (!json::i64(result.body, "\"retCode\"", code) || code != 0) {
      error = "bybit: " + std::string(json::scalar(result.body, "\"retMsg\""));
      return false;
    }
  }
  return true;
}

}  // namespace

std::uint64_t wall_ns() noexcept {
  return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::system_clock::now().time_since_epoch()).count());
}

std::string hmac_sha256_hex(std::string_view secret, std::string_view payload) {
  unsigned int length = EVP_MAX_MD_SIZE;
  unsigned char digest[EVP_MAX_MD_SIZE];
  if (HMAC(EVP_sha256(), secret.data(), static_cast<int>(secret.size()),
           reinterpret_cast<const unsigned char*>(payload.data()), payload.size(), digest, &length) == nullptr)
    return {};
  static constexpr char kHex[] = "0123456789abcdef";
  std::string out;
  out.reserve(length * 2);
  for (unsigned int i = 0; i < length; ++i) {
    out.push_back(kHex[digest[i] >> 4]);
    out.push_back(kHex[digest[i] & 0x0F]);
  }
  return out;
}

HttpClient::HttpClient() : curl_(curl_easy_init()) {}
HttpClient::~HttpClient() {
  if (curl_ != nullptr) curl_easy_cleanup(static_cast<CURL*>(curl_));
}

HttpResult HttpClient::get(const std::string& url, const std::vector<std::string>& headers) {
  HttpResult result;
  auto* curl = static_cast<CURL*>(curl_);
  if (curl == nullptr) { result.error = "curl initialization failed"; return result; }
  curl_slist* list = nullptr;
  for (const auto& header : headers) list = curl_slist_append(list, header.c_str());
  curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
  curl_easy_setopt(curl, CURLOPT_HTTPGET, 1L);
  curl_easy_setopt(curl, CURLOPT_HTTPHEADER, list);
  curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, append);
  curl_easy_setopt(curl, CURLOPT_WRITEDATA, &result.body);
  curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 10'000L);
  curl_easy_setopt(curl, CURLOPT_USERAGENT, "trading-algos-crossex/0.1");
  curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
  curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L);
  curl_easy_setopt(curl, CURLOPT_TCP_KEEPALIVE, 1L);
  curl_easy_setopt(curl, CURLOPT_TCP_NODELAY, 1L);
  curl_easy_setopt(curl, CURLOPT_ACCEPT_ENCODING, "");
  const auto start = std::chrono::steady_clock::now();
  const auto code = curl_easy_perform(curl);
  result.elapsed_ns = static_cast<std::uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - start).count());
  curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &result.status);
  curl_easy_setopt(curl, CURLOPT_HTTPHEADER, nullptr);
  curl_slist_free_all(list);
  if (code != CURLE_OK) {
    result.error = curl_easy_strerror(code);
    return result;
  }
  result.ok = result.status == 200;
  if (!result.ok) result.error = "HTTP " + std::to_string(result.status) + " " + result.body.substr(0, 200);
  return result;
}

Credentials load_credentials(Venue venue) {
  if (venue == Venue::binance) return {env("CXA_BINANCE_API_KEY"), env("CXA_BINANCE_API_SECRET")};
  return {env("CXA_BYBIT_API_KEY"), env("CXA_BYBIT_API_SECRET")};
}

HttpResult signed_get(HttpClient& http, Venue venue, const Credentials& credentials, std::string_view base,
                      std::string_view path, std::string_view query) {
  const auto timestamp = std::to_string(wall_ns() / 1'000'000);
  if (venue == Venue::binance) {
    auto signed_query = std::string(query);
    if (!signed_query.empty()) signed_query += '&';
    signed_query += "recvWindow=" + std::string(kRecvWindow) + "&timestamp=" + timestamp;
    const auto signature = hmac_sha256_hex(credentials.secret, signed_query);
    return http.get(std::string(base) + std::string(path) + "?" + signed_query + "&signature=" + signature,
                    {"X-MBX-APIKEY: " + credentials.key});
  }
  const auto payload = timestamp + credentials.key + std::string(kRecvWindow) + std::string(query);
  const auto signature = hmac_sha256_hex(credentials.secret, payload);
  auto url = std::string(base) + std::string(path);
  if (!query.empty()) url += "?" + std::string(query);
  return http.get(url, {"X-BAPI-API-KEY: " + credentials.key, "X-BAPI-TIMESTAMP: " + timestamp,
                        "X-BAPI-RECV-WINDOW: " + std::string(kRecvWindow), "X-BAPI-SIGN: " + signature});
}

bool verify_read_only(HttpClient& http, Venue venue, const Credentials& credentials, std::string& detail) {
  if (!credentials.present()) {
    detail = std::string(venue_name(venue)) + ": API key/secret not set";
    return false;
  }
  if (venue == Venue::binance) {
    const auto result = signed_get(http, venue, credentials, kBinanceSpot, "/sapi/v1/account/apiRestrictions", "");
    if (!api_ok(venue, result, detail)) return false;
    bool reading = false;
    if (!json::boolean(result.body, "\"enableReading\"", reading) || !reading) {
      detail = "binance: key cannot read account data";
      return false;
    }
    // Every other enable*/permits* permission must be off.
    std::string_view body(result.body);
    for (std::size_t pos = 0; (pos = body.find('"', pos)) != std::string_view::npos;) {
      const auto end = body.find('"', pos + 1);
      if (end == std::string_view::npos) break;
      const auto key = body.substr(pos + 1, end - pos - 1);
      pos = end + 1;
      if (!(key.starts_with("enable") || key.starts_with("permits")) || key == "enableReading" ||
          key == "enableFixReadOnly")
        continue;
      const auto quoted = "\"" + std::string(key) + "\"";
      bool enabled = false;
      if (json::boolean(body, quoted, enabled) && enabled) {
        detail = "binance: key has permission " + std::string(key) + "; shadow needs a read-only key";
        return false;
      }
    }
    detail = "binance: read-only";
    return true;
  }
  const auto result = signed_get(http, venue, credentials, kBybit, "/v5/user/query-api", "");
  if (!api_ok(venue, result, detail)) return false;
  std::int64_t read_only = 0;
  if (!json::i64(result.body, "\"readOnly\"", read_only) || read_only != 1) {
    detail = "bybit: key is not read-only (readOnly != 1)";
    return false;
  }
  detail = "bybit: read-only";
  return true;
}

bool fetch_taker_fees(HttpClient& http, Venue venue, const Credentials& credentials,
                      const std::vector<Instrument>& basket, std::vector<std::int64_t>& taker_ppm,
                      std::string& error) {
  taker_ppm.assign(basket.size(), 0);
  const auto v = index(venue);
  if (venue == Venue::bybit) {
    const auto result = signed_get(http, venue, credentials, kBybit, "/v5/account/fee-rate", "category=linear");
    if (!api_ok(venue, result, error)) return false;
    std::string_view body(result.body);
    for (std::size_t i = 0; i < basket.size(); ++i) {
      const auto needle = "\"symbol\":\"" + basket[i].symbol[v] + "\"";
      const auto pos = body.find(needle);
      if (pos == std::string_view::npos ||
          !json::rate_ppm(json::enclosing_object(body, pos), "\"takerFeeRate\"", taker_ppm[i])) {
        error = "bybit: no fee rate for " + basket[i].symbol[v];
        return false;
      }
    }
    return true;
  }
  for (std::size_t i = 0; i < basket.size(); ++i) {
    const auto result = signed_get(http, venue, credentials, kBinanceFutures, "/fapi/v1/commissionRate",
                                   "symbol=" + basket[i].symbol[v]);
    if (!api_ok(venue, result, error)) return false;
    if (!json::rate_ppm(result.body, "\"takerCommissionRate\"", taker_ppm[i])) {
      error = "binance: no commission rate for " + basket[i].symbol[v];
      return false;
    }
  }
  return true;
}

bool load_instruments(HttpClient& http, std::vector<Instrument>& basket, std::string& error) {
  const auto binance = http.get(std::string(kBinanceFutures) + "/fapi/v1/exchangeInfo");
  if (!api_ok(Venue::binance, binance, error)) return false;
  for (auto& instrument : basket) {
    if (!parse_binance_instrument(binance.body, instrument, error)) return false;
    const auto bybit = http.get(std::string(kBybit) + "/v5/market/instruments-info?category=linear&symbol=" +
                                instrument.symbol[index(Venue::bybit)]);
    if (!api_ok(Venue::bybit, bybit, error)) return false;
    if (!parse_bybit_instrument(bybit.body, instrument, error)) return false;
    if (!finalize_instrument(instrument, error)) return false;
  }
  return true;
}

HttpResult signed_probe(HttpClient& http, Venue venue, const Credentials& credentials) {
  if (venue == Venue::binance)
    return signed_get(http, venue, credentials, kBinanceFutures, "/fapi/v2/balance", "");
  return signed_get(http, venue, credentials, kBybit, "/v5/account/wallet-balance", "accountType=UNIFIED");
}

HttpResult time_probe(HttpClient& http, Venue venue, std::uint64_t& server_ms) {
  auto result = venue == Venue::binance ? http.get(std::string(kBinanceFutures) + "/fapi/v1/time")
                                        : http.get(std::string(kBybit) + "/v5/market/time");
  server_ms = 0;
  if (result.ok) {
    if (venue == Venue::binance) json::u64(result.body, "\"serverTime\"", server_ms);
    else json::u64(result.body, "\"time\"", server_ms);
  }
  return result;
}

}  // namespace cxa
