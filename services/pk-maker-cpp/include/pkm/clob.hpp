#pragma once

#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

#include "pkm/crypto.hpp"
#include "pkm/eip712.hpp"

// The CLOB's wire formats: the POST /order body, L2 headers, the order math of
// `order_builder.rs`, and the answers we act on.
namespace pkm::clob {

// Prices are integer ticks of 0.01 and sizes whole shares (the maker quotes
// whole shares on 1¢ markets), so the amounts are exact integers.
struct Amounts {
  std::uint64_t maker = 0;  // pUSD paid, 1e6 units
  std::uint64_t taker = 0;  // shares received, 1e6 units
};
// A BUY of `shares` at `price_cents`.
constexpr Amounts buy_amounts(std::int64_t price_cents, std::int64_t shares) noexcept {
  return {static_cast<std::uint64_t>(price_cents * shares) * 10'000ULL, static_cast<std::uint64_t>(shares) * 1'000'000ULL};
}

// {"order":{…,"signature":…},"orderType":"GTC","owner":…,"postOnly":true}, the
// SDK's field order. `token` is the token id in decimal, as the window holds it.
// `out` is cleared, and keeps its capacity.
void write_order_body(std::string& out, const eip712::OrderV2& order, std::string_view token, std::string_view signature,
                      std::string_view owner, bool post_only);
void write_cancel_body(std::string& out, std::string_view order_id);

struct Credentials {
  std::string api_key;
  std::vector<std::uint8_t> secret;  // URL-safe base64 decoded
  std::string passphrase;
  std::string address;  // EIP-55 address of the signing key
};

// POLY_* header lines for one request, each ending CRLF. The HMAC is over
// timestamp_s + method + path + body (single quotes read as double quotes).
class L2Signer final {
 public:
  explicit L2Signer(Credentials credentials);
  void write_headers(std::string& out, std::int64_t timestamp_s, std::string_view method, std::string_view path,
                     std::string_view body);
  [[nodiscard]] std::string signature(std::int64_t timestamp_s, std::string_view method, std::string_view path,
                                      std::string_view body);
  [[nodiscard]] const Credentials& credentials() const noexcept { return credentials_; }

 private:
  Credentials credentials_;
  HmacSha256 hmac_;
  std::string scratch_;
};

struct PostResult {
  bool ok = false;
  std::string order_id;
  std::string error;
};
// The POST /order answer: {"success":true,"orderID":"0x…",…} or an error.
PostResult parse_post_response(int status, std::string_view body);

struct CancelResult {
  bool ok = false;
  std::string error;
};
// DELETE /order: {"canceled":["0x…"],"not_canceled":{"0x…":"reason"}}.
CancelResult parse_cancel_response(int status, std::string_view body, std::string_view order_id);

// "… not enough balance … balance: 385700, order amount: 2700000" → 0.3857.
bool free_balance(std::string_view error, double& out) noexcept;

}  // namespace pkm::clob
