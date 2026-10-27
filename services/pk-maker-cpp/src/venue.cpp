#include "pkm/venue.hpp"

#include <openssl/rand.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <thread>

#include "pkm/feeds.hpp"
#include "pkm/json.hpp"
#include "pkm/net.hpp"

namespace pkm::venue {
namespace {

std::int64_t unix_now() { return net::now_ns() / 1'000'000'000LL; }

std::string snippet(std::string_view body) { return std::string(body.substr(0, 300)); }

std::string http_error(const http::Response& r, std::string_view what) {
  if (r.status == 0) return std::string(what) + ": " + r.error;
  return std::string(what) + ": http " + std::to_string(r.status) + " " + snippet(r.body);
}

// A list Gamma sends as a JSON-encoded string: "[\"Up\", \"Down\"]".
std::vector<std::string> string_list(const json::Value& v) {
  std::vector<std::string> out;
  json::Document inner;
  const std::string text = v.string();
  if (!inner.parse(text)) return out;
  for (const auto item : inner.root()) out.push_back(item.string());
  return out;
}

}  // namespace

std::string poly_slug(std::string_view asset, std::int64_t open_unix) {
  std::string s(asset);
  for (auto& c : s) c = static_cast<char>(c >= 'A' && c <= 'Z' ? c - 'A' + 'a' : c);
  return s + "-updown-15m-" + std::to_string(open_unix);
}

bool load_wallet(const Config& config, Wallet& out, std::string& error) {
  if (config.private_key.empty()) {
    error = "POLY_PRIVATE_KEY (or POLY_PRIVATE_KEY_FILE) is not set";
    return false;
  }
  if (!out.signer.load(config.private_key, error)) return false;
  if (!parse_address(config.funder, out.funder)) {
    error = "POLY_FUNDER must be the deposit wallet's address";
    return false;
  }
  out.funder_checksum = checksum_address(out.funder);
  out.eoa_checksum = checksum_address(out.signer.address());
  return true;
}

std::string l1_headers(const Wallet& wallet, std::int64_t timestamp_s, std::uint64_t nonce) {
  const Bytes32 digest = eip712::clob_auth_digest(wallet.signer.address(), timestamp_s, nonce);
  return "POLY_ADDRESS: " + to_hex(wallet.signer.address()) + "\r\nPOLY_SIGNATURE: " + to_hex(wallet.signer.sign(digest)) +
         "\r\nPOLY_TIMESTAMP: " + std::to_string(timestamp_s) + "\r\nPOLY_NONCE: " + std::to_string(nonce) + "\r\n";
}

std::uint64_t random_salt() noexcept {
  std::uint64_t v = 0;
  RAND_bytes(reinterpret_cast<unsigned char*>(&v), sizeof(v));
  return v & ((1ULL << 53) - 1);  // the CLOB reads the salt as a JSON number
}

eip712::OrderV2 make_order(const Wallet& wallet, const Bytes32& token_id, std::int64_t price_cents, std::int64_t shares,
                           std::uint64_t salt, std::uint64_t timestamp_ms) {
  eip712::OrderV2 o;
  o.salt = salt;
  o.maker = wallet.funder;
  o.signer = wallet.funder;
  o.token_id = token_id;
  const auto amounts = clob::buy_amounts(price_cents, shares);
  o.maker_amount = amounts.maker;
  o.taker_amount = amounts.taker;
  o.side = 0;
  o.signature_type = eip712::SignatureType::deposit_wallet;
  o.timestamp_ms = timestamp_ms;
  return o;
}

Clob::Clob(const Config& config, const Wallet& wallet, clob::Credentials creds)
    : config_(config), wallet_(wallet), l2_(std::move(creds)), http_(config.clob_url, kUserAgent, 10'000) {}

bool Clob::derive_credentials(const Config& config, const Wallet& wallet, clob::Credentials& out, std::string& error) {
  out.address = wallet.eoa_checksum;
  if (!config.clob_api_key.empty()) {
    out.api_key = config.clob_api_key;
    out.passphrase = config.clob_api_passphrase;
    if (!base64_decode(config.clob_api_secret, out.secret, true) || out.secret.empty()) {
      error = "POLY_CLOB_API_SECRET is not URL-safe base64";
      return false;
    }
    return true;
  }
  http::Client http(config.clob_url, kUserAgent, 10'000);
  auto r = http.get("/auth/derive-api-key", l1_headers(wallet, unix_now(), 0));
  if (!r.ok()) r = http.request("POST", "/auth/api-key", l1_headers(wallet, unix_now(), 0), "");
  json::Document doc;
  if (!r.ok() || !doc.parse(r.body)) {
    error = http_error(r, "CLOB API key");
    return false;
  }
  out.api_key = doc.root()["apiKey"].string();
  out.passphrase = doc.root()["passphrase"].string();
  if (out.api_key.empty() || !base64_decode(doc.root()["secret"].string(), out.secret, true)) {
    error = "CLOB API key answer without apiKey/secret";
    return false;
  }
  return true;
}

http::Response Clob::authed(std::string_view method, std::string_view path, std::string_view query, std::string_view body) {
  headers_.clear();
  l2_.write_headers(headers_, unix_now(), method, path, body);
  std::string full(path);
  if (!query.empty()) full.append("?").append(query);
  return http_.request(method, full, headers_, body);
}

bool Clob::version(int& out, std::string& error) {
  const auto r = http_.get("/version");
  json::Document doc;
  if (!r.ok() || !doc.parse(r.body)) {
    error = http_error(r, "GET /version");
    return false;
  }
  out = static_cast<int>(doc.root()["version"].integer_or(0));
  return true;
}

bool Clob::cash(double& out, std::string& error) {
  const auto r = authed("GET", "/balance-allowance", "asset_type=COLLATERAL&signature_type=" + std::to_string(config_.signature_type));
  json::Document doc;
  if (!r.ok() || !doc.parse(r.body)) {
    error = http_error(r, "cash");
    return false;
  }
  out = doc.root()["balance"].number_or(0) / 1e6;
  return true;
}

bool Clob::token_balance(const std::string& token, double& out, std::string& error) {
  const auto r = authed("GET", "/balance-allowance",
                        "asset_type=CONDITIONAL&token_id=" + token + "&signature_type=" + std::to_string(config_.signature_type));
  json::Document doc;
  if (!r.ok() || !doc.parse(r.body)) {
    error = http_error(r, "token balance");
    return false;
  }
  out = doc.root()["balance"].number_or(0) / 1e6;
  return true;
}

bool Clob::open_orders(std::vector<OpenOrder>& out, std::string& error) {
  out.clear();
  std::string cursor;
  for (int page = 0; page < 20; ++page) {
    const auto r = authed("GET", "/data/orders", cursor.empty() ? std::string() : "next_cursor=" + cursor);
    json::Document doc;
    if (!r.ok() || !doc.parse(r.body)) {
      error = http_error(r, "open orders");
      return false;
    }
    const auto root = doc.root();
    const auto list = root.is_array() ? root : root["data"];
    for (const auto o : list) {
      OpenOrder x;
      x.id = o["id"].string();
      x.asset_id = o["asset_id"].string();
      x.matched = o["size_matched"].number_or(0);
      x.original = o["original_size"].number_or(0);
      x.price = o["price"].number_or(0);
      x.created_s = o["created_at"].integer_or(0);
      if (!x.id.empty()) out.push_back(std::move(x));
    }
    cursor = root.is_object() ? root["next_cursor"].string() : std::string();
    if (cursor.empty() || cursor == "LTE=") return true;
  }
  return true;
}

bool Clob::order_state(const std::string& id, std::optional<OrderState>& out, std::string& error) {
  const auto r = authed("GET", "/data/order/" + id);
  json::Document doc;
  out.reset();
  if (!r.ok() || !doc.parse(r.body)) {
    error = http_error(r, "order " + id);
    return false;
  }
  auto v = doc.root();
  if (v.is_null()) return true;
  if (v["order"].is_object()) v = v["order"];
  const auto status = v["status"].string();
  if (status.empty()) {
    error = "order " + id + ": no status in " + snippet(r.body);
    return false;
  }
  OrderState s;
  s.matched = v["size_matched"].number_or(0);
  s.live = status == "LIVE" || status == "live";
  out = s;
  return true;
}

bool Clob::cancel_all(int& cancelled, std::string& error) {
  const auto r = authed("DELETE", "/cancel-all");
  json::Document doc;
  if (!r.ok() || !doc.parse(r.body)) {
    error = http_error(r, "cancel-all");
    return false;
  }
  cancelled = static_cast<int>(doc.root()["canceled"].size());
  return true;
}

bool Clob::cancel(const std::string& id, std::string& error) {
  std::string body;
  clob::write_cancel_body(body, id);
  const auto r = authed("DELETE", "/order", {}, body);
  const auto result = clob::parse_cancel_response(r.status, r.body, id);
  if (!result.ok) error = r.status == 0 ? r.error : result.error;
  return result.ok;
}

bool Clob::book(const std::string& token, BookMeta& out, std::string& error) {
  const auto r = http_.get("/book?token_id=" + token);
  json::Document doc;
  if (!r.ok() || !doc.parse(r.body)) {
    error = http_error(r, "book");
    return false;
  }
  const auto root = doc.root();
  out.tick = root["tick_size"].number_or(0.01);
  out.min_size = root["min_order_size"].number_or(5);
  out.neg_risk = root["neg_risk"].boolean_or(false);
  out.best_bid.reset();
  out.best_ask.reset();
  for (const auto l : root["bids"]) {
    const double p = l["price"].number_or(0);
    if (!out.best_bid || p > *out.best_bid) out.best_bid = p;
  }
  for (const auto l : root["asks"]) {
    const double p = l["price"].number_or(0);
    if (!out.best_ask || p < *out.best_ask) out.best_ask = p;
  }
  return true;
}

bool Clob::check_headers(std::string& error) {
  const auto r = authed("POST", "/order", {}, "{}");
  if (r.status == 400) return true;
  error = http_error(r, "signed empty POST /order (want 400)");
  return false;
}

clob::PostResult Clob::post(const std::string& token, std::int64_t price_cents, std::int64_t shares, bool neg_risk) {
  Bytes32 id{};
  clob::PostResult result;
  if (!decimal_to_u256(token, id)) {
    result.error = "token id is not decimal";
    return result;
  }
  const auto order = make_order(wallet_, id, price_cents, shares, random_salt(), static_cast<std::uint64_t>(net::now_ns() / 1'000'000));
  const std::string signature = eip712::sign_order(wallet_.signer, order, neg_risk);
  std::string body;
  clob::write_order_body(body, order, token, signature, l2_.credentials().api_key, true);
  const auto r = authed("POST", "/order", {}, body);
  if (r.status == 0) {
    result.error = r.error;
    return result;
  }
  return clob::parse_post_response(r.status, r.body);
}

Relayer::Relayer(const Config& config, const Wallet& wallet)
    : config_(config),
      wallet_(wallet),
      api_key_(config.relayer_api_key),
      api_key_address_(config.relayer_api_key_address.empty() ? wallet.eoa_checksum : config.relayer_api_key_address),
      http_(config.relayer_url, kUserAgent, 15'000) {
  parse_address(config.collateral, collateral_);
}

std::string Relayer::auth_headers() const {
  return "RELAYER_API_KEY: " + api_key_ + "\r\nRELAYER_API_KEY_ADDRESS: " + api_key_address_ + "\r\n";
}

bool Relayer::redeem(const std::string& condition_id, std::string& out) {
  Bytes32 condition{};
  if (!parse_hex(condition_id, condition.data(), 32)) {
    out = "condition id is not 32 bytes of hex";
    return false;
  }
  auto r = http_.get("/v1/account/transactions/params?address=" + wallet_.eoa_checksum + "&type=WALLET", auth_headers());
  json::Document doc;
  Bytes32 nonce{};
  std::string nonce_text;
  if (!r.ok() || !doc.parse(r.body) || !decimal_to_u256((nonce_text = doc.root()["nonce"].string()), nonce)) {
    out = http_error(r, "relayer nonce");
    return false;
  }
  const std::uint64_t deadline = static_cast<std::uint64_t>(unix_now() + 300);
  eip712::Call call;
  parse_address(eip712::kCtfCollateralAdapter, call.target);
  call.data = eip712::redeem_positions_calldata(collateral_, condition);
  const Bytes32 digest = eip712::batch_digest(wallet_.funder, nonce, deadline, {call});
  const Signature65 sig = wallet_.signer.sign(digest);
  Address factory{};
  parse_address(eip712::kDepositWalletFactory, factory);
  std::string body = "{\"type\":\"WALLET\",\"from\":\"" + wallet_.eoa_checksum + "\",\"to\":\"" + checksum_address(factory) +
                     "\",\"nonce\":\"" + nonce_text + "\",\"signature\":\"" + to_hex(sig) +
                     "\",\"metadata\":\"Redeem position\",\"depositWalletParams\":{\"depositWallet\":\"" +
                     wallet_.funder_checksum + "\",\"deadline\":\"" + std::to_string(deadline) + "\",\"calls\":[{\"target\":\"" +
                     checksum_address(call.target) + "\",\"value\":\"0\",\"data\":\"" + to_hex(call.data.data(), call.data.size()) +
                     "\"}]}}";
  r = http_.request("POST", "/submit", auth_headers(), body);
  if (!r.ok() || !doc.parse(r.body)) {
    out = http_error(r, "relayer submit");
    return false;
  }
  const std::string id = doc.root()["transactionID"].string();
  if (id.empty()) {
    out = "relayer submit without a transactionID: " + snippet(r.body);
    return false;
  }
  std::string last;
  for (int i = 0; i < 60; ++i) {
    std::this_thread::sleep_for(std::chrono::seconds(2));
    r = http_.get("/v1/account/transactions/" + id, auth_headers());
    if (!r.ok() || !doc.parse(r.body)) {
      last = http_error(r, "relayer poll");
      continue;
    }
    auto v = doc.root();
    if (v.is_array()) v = v.at(0);
    const std::string state = v["state"].string();
    if (state == "STATE_CONFIRMED") {
      out = v["transaction_hash"].string();
      if (out.empty()) out = v["transactionHash"].string();
      return !out.empty();
    }
    if (state == "STATE_FAILED" || state == "STATE_INVALID") {
      std::string why = v["error_msg"].string();
      if (why.empty()) why = v["errorMsg"].string();
      out = "relayer transaction " + id + " " + state + ": " + (why.empty() ? "no error message" : why);
      return false;
    }
    last = state;
  }
  out = "relayer transaction " + id + " not confirmed after 120 s (last: " + last + ")";
  return false;
}

Discovery::Discovery(const Config& config)
    : gamma_(config.gamma_url, kUserAgent, 10'000), kalshi_(config.kalshi_rest_url, kUserAgent, 10'000) {}

bool Discovery::get_json(http::Client& client, const std::string& path, std::string& body, std::string& error) {
  for (int attempt = 0;; ++attempt) {
    const auto r = client.get(path);
    if (r.status == 429 && attempt < 4) {
      unsigned char jitter = 0;
      RAND_bytes(&jitter, 1);
      const double s = 0.5 * std::pow(2.0, attempt) * (1.0 + jitter / 255.0);
      std::this_thread::sleep_for(std::chrono::milliseconds(static_cast<long>(s * 1000)));
      continue;
    }
    if (!r.ok()) {
      error = http_error(r, "GET " + path);
      return false;
    }
    body = r.body;
    return true;
  }
}

bool Discovery::poly_market(const std::string& asset, std::int64_t open_unix, std::string& body, std::string& error,
                            bool fresh) {
  std::string path = "/events?slug=" + poly_slug(asset, open_unix);
  // Gamma sits behind CloudFront with max-age=300: a cached answer kept a
  // resolved window "open" for up to 5 minutes. A unique query is a cache miss.
  if (fresh) path += "&_=" + std::to_string(net::now_ns() / 1'000'000);
  return get_json(gamma_, path, body, error);
}

bool Discovery::discover(const std::string& asset, std::int64_t open_unix, std::optional<engine::Window>& out, std::string& error) {
  out.reset();
  std::string body;
  if (!poly_market(asset, open_unix, body, error)) return false;
  json::Document poly;
  if (!poly.parse(body)) {
    error = "Gamma answer is not JSON";
    return false;
  }
  const auto market = poly.root().at(0)["markets"].at(0);
  if (!market.is_object()) return true;  // not listed yet
  std::int64_t start = 0;
  if (const auto s = market["eventStartTime"]; s.is_string() && (!feeds::iso_to_unix(s.raw(), start) || start != open_unix)) {
    error = asset + " " + std::to_string(open_unix) + ": Polymarket eventStartTime is " + s.string();
    return false;
  }
  auto outcomes = string_list(market["outcomes"]);
  for (auto& o : outcomes)
    for (auto& ch : o) ch = static_cast<char>(ch >= 'A' && ch <= 'Z' ? ch - 'A' + 'a' : ch);
  const auto tokens = string_list(market["clobTokenIds"]);
  if (outcomes != std::vector<std::string>{"up", "down"} || tokens.size() != 2) {
    error = asset + " " + std::to_string(open_unix) + ": outcomes are not [Up, Down]";
    return false;
  }

  const std::int64_t close = open_unix + engine::kWindowS;
  const auto key = std::make_pair(asset, close);
  if (!kalshi_cache_.contains(key)) {
    std::string kb;
    if (!get_json(kalshi_, "/markets?series_ticker=KX" + asset + "15M&min_close_ts=" + std::to_string(close - 1) +
                               "&max_close_ts=" + std::to_string(close + engine::kWindowS + 1),
                  kb, error))
      return false;
    json::Document k;
    if (!k.parse(kb)) {
      error = "Kalshi answer is not JSON";
      return false;
    }
    std::map<std::int64_t, std::vector<std::string>> by_close;
    for (const auto m : k.root()["markets"]) {
      std::int64_t c = 0;
      std::int64_t o = 0;
      if (feeds::iso_to_unix(m["close_time"].raw(), c) || (feeds::iso_to_unix(m["open_time"].raw(), o) && (c = o + engine::kWindowS)))
        by_close[c].push_back(m["ticker"].string() + "\n" + m["open_time"].string());
    }
    for (auto& [c, found] : by_close) {
      if (found.size() > 1) {
        error = asset + " " + std::to_string(c) + ": " + std::to_string(found.size()) + " Kalshi markets";
        return false;
      }
      kalshi_cache_[{asset, c}] = found[0];
    }
    for (auto it = kalshi_cache_.begin(); it != kalshi_cache_.end();)
      it = it->first.second < close - 6 * 3600 ? kalshi_cache_.erase(it) : std::next(it);
  }
  const auto it = kalshi_cache_.find(key);
  if (it == kalshi_cache_.end()) return true;  // not listed yet
  const auto nl = it->second.find('\n');
  std::int64_t kopen = 0;
  if (!feeds::iso_to_unix(it->second.substr(nl + 1), kopen) || kopen != open_unix) {
    error = asset + " " + std::to_string(open_unix) + ": Kalshi open_time " + it->second.substr(nl + 1);
    return false;
  }
  engine::Window w;
  w.asset = asset;
  w.open_unix = open_unix;
  w.kalshi_ticker = it->second.substr(0, nl);
  w.up_token = tokens[0];
  w.down_token = tokens[1];
  w.condition_id = market["conditionId"].string();
  w.neg_risk = market["negRisk"].boolean_or(false);
  out = w;
  return true;
}

bool Discovery::outcome(const std::string& asset, std::int64_t open_unix, std::string& out, std::string& error) {
  out.clear();
  std::string body;
  if (!poly_market(asset, open_unix, body, error, true)) return false;
  json::Document doc;
  if (!doc.parse(body)) {
    error = "Gamma answer is not JSON";
    return false;
  }
  const auto market = doc.root().at(0)["markets"].at(0);
  if (!market.is_object() || !market["closed"].boolean_or(false)) return true;
  const auto prices = string_list(market["outcomePrices"]);
  if (prices.size() != 2) return true;
  const double a = std::atof(prices[0].c_str());
  const double b = std::atof(prices[1].c_str());
  if (a == 1.0 && b == 0.0) out = "yes";
  else if (a == 0.0 && b == 1.0) out = "no";
  return true;
}

}  // namespace pkm::venue
