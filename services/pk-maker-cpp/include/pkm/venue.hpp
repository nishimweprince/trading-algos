#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include "pkm/clob.hpp"
#include "pkm/config.hpp"
#include "pkm/crypto.hpp"
#include "pkm/engine.hpp"
#include "pkm/http.hpp"

// Blocking REST clients for the control thread: the CLOB's account routes,
// the relayer (deposit-wallet redemption), and window discovery on Gamma and
// Kalshi. Ports of pk-exec's clob.rs/relayer.rs and pk-feeds' discovery.rs.
namespace pkm::venue {

inline constexpr const char* kUserAgent = "pk-maker-cpp/0.1 (+trading-algos)";

// The signing key and the deposit wallet it owns.
struct Wallet {
  EcSigner signer;
  Address funder{};
  std::string funder_checksum;
  std::string eoa_checksum;
};
bool load_wallet(const Config& config, Wallet& out, std::string& error);

// L1 (EIP-712 ClobAuth) headers for creating or deriving the API key.
std::string l1_headers(const Wallet& wallet, std::int64_t timestamp_s, std::uint64_t nonce);

class Clob final {
 public:
  Clob(const Config& config, const Wallet& wallet, clob::Credentials creds);

  // GET /auth/derive-api-key, else POST /auth/api-key.
  static bool derive_credentials(const Config& config, const Wallet& wallet, clob::Credentials& out, std::string& error);

  bool version(int& out, std::string& error);
  bool cash(double& out, std::string& error);  // pUSD the CLOB lets us spend
  bool token_balance(const std::string& token, double& out, std::string& error);
  struct OpenOrder {
    std::string id;
    std::string asset_id;
    double matched = 0;
    double original = 0;
    double price = 0;
    std::int64_t created_s = 0;
  };
  bool open_orders(std::vector<OpenOrder>& out, std::string& error);
  // nullopt when the venue answers null (not indexed yet, or long gone).
  struct OrderState {
    double matched = 0;
    bool live = false;
  };
  bool order_state(const std::string& id, std::optional<OrderState>& out, std::string& error);
  bool cancel_all(int& cancelled, std::string& error);
  bool cancel(const std::string& id, std::string& error);
  struct BookMeta {
    double tick = 0.01;
    double min_size = 5;
    bool neg_risk = false;
    std::optional<double> best_bid;
    std::optional<double> best_ask;
  };
  bool book(const std::string& token, BookMeta& out, std::string& error);
  // A signed, empty POST /order: 400 means the headers were accepted.
  bool check_headers(std::string& error);
  // A post-only GTC buy (used by `pkm check`); the order id or the refusal.
  clob::PostResult post(const std::string& token, std::int64_t price_cents, std::int64_t shares, bool neg_risk);

  [[nodiscard]] const clob::Credentials& credentials() const noexcept { return l2_.credentials(); }

 private:
  http::Response authed(std::string_view method, std::string_view path, std::string_view query = {},
                        std::string_view body = {});

  const Config& config_;
  const Wallet& wallet_;
  clob::L2Signer l2_;
  http::Client http_;
  std::string headers_;
};

// Builds and signs one order (deposit wallet: maker = signer = the wallet).
eip712::OrderV2 make_order(const Wallet& wallet, const Bytes32& token_id, std::int64_t price_cents, std::int64_t shares,
                           std::uint64_t salt, std::uint64_t timestamp_ms);
std::uint64_t random_salt() noexcept;

class Relayer final {
 public:
  Relayer(const Config& config, const Wallet& wallet);
  [[nodiscard]] bool configured() const noexcept { return !api_key_.empty(); }
  // Redeems both index sets of a standard CTF market into pUSD; the tx hash.
  bool redeem(const std::string& condition_id, std::string& tx_or_error);

 private:
  std::string auth_headers() const;
  const Config& config_;
  const Wallet& wallet_;
  std::string api_key_;
  std::string api_key_address_;
  Address collateral_{};
  http::Client http_;
};

class Discovery final {
 public:
  explicit Discovery(const Config& config);
  // The window on both venues, nullopt while either has not listed it.
  bool discover(const std::string& asset, std::int64_t open_unix, std::optional<engine::Window>& out, std::string& error);
  // "yes"/"no" once Polymarket resolved the window, "" before.
  bool outcome(const std::string& asset, std::int64_t open_unix, std::string& out, std::string& error);

 private:
  bool get_json(http::Client& client, const std::string& path, std::string& body, std::string& error);
  bool poly_market(const std::string& asset, std::int64_t open_unix, std::string& body, std::string& error);

  http::Client gamma_;
  http::Client kalshi_;
  // (asset, close) → market JSON, one query covering this window and the next.
  std::map<std::pair<std::string, std::int64_t>, std::string> kalshi_cache_;
};

std::string poly_slug(std::string_view asset, std::int64_t open_unix);
inline std::int64_t window_open(std::int64_t unix_s) noexcept { return unix_s - unix_s % engine::kWindowS; }

}  // namespace pkm::venue
