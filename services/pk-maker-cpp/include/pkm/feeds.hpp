#pragma once

#include <cstdint>
#include <map>
#include <set>
#include <string>
#include <string_view>
#include <vector>

#include "pkm/engine.hpp"
#include "pkm/json.hpp"

// Venue frames into the engine's books, and the subscription protocols, with
// no sockets: the runtime owns the I/O. Ports of pk-engine's `parse.rs`
// (KalshiMsg, PolyMsg) and pk-feeds' `ws.rs` session rules.
namespace pkm::feeds {

// --- Kalshi ------------------------------------------------------------------

// The orderbook_delta subscription on one socket: one sid, tickers added and
// removed with update_subscription, `seq` contiguous per sid (acks included).
class KalshiSession final {
 public:
  void reset() noexcept;
  // Messages to send so the subscription becomes `wanted`.
  std::vector<std::string> sync(const std::set<std::string>& wanted);
  enum class Verdict : std::uint8_t { ok, gap, error };
  // Bookkeeping for every frame (sid from "subscribed", seq checks).
  Verdict on_frame(const json::Value& root, std::vector<std::string>& to_send);
  [[nodiscard]] const std::set<std::string>& subscribed() const noexcept { return subscribed_; }
  [[nodiscard]] std::int64_t sid() const noexcept { return sid_; }
  std::string last_error;

 private:
  std::int64_t next_id() noexcept { return ++cmd_id_; }
  std::int64_t sid_ = -1;
  bool subscribe_in_flight_ = false;
  std::int64_t cmd_id_ = 0;
  std::map<std::int64_t, std::int64_t> seq_;
  std::set<std::string> subscribed_;
  std::set<std::string> pending_add_;
};

// orderbook_snapshot / orderbook_delta into the coin's Kalshi book (YES bids;
// a NO bid at p is a YES ask at 1 − p). The coin touched, or -1.
int apply_kalshi(const json::Value& root, engine::Engine& engine, std::int64_t recv_ns);

// --- Polymarket --------------------------------------------------------------

struct Resolution {
  std::string condition_id;
  std::string winning_token;
};

// A market-channel frame (one object or a list): `book` and `price_change`
// update books, `market_resolved` is returned; `best_bid_ask` is ignored
// (one update stale). Sets touched[coin] for each coin changed.
void apply_poly(const json::Value& root, engine::Engine& engine, std::int64_t recv_ns, std::vector<char>& touched,
                std::vector<Resolution>* resolved);

// First subscription carries "type":"market"; later ones "operation".
std::string poly_subscribe(const std::vector<std::string>& tokens, bool initial);
std::string poly_unsubscribe(const std::vector<std::string>& tokens);

// User channel: {"auth":{apiKey,secret,passphrase},"markets":[condition ids],"type":"user"}.
std::string user_subscribe(std::string_view api_key, std::string_view secret_b64, std::string_view passphrase,
                           const std::vector<std::string>& condition_ids);

// An order's state from the user channel: `order` events carry the order's
// own total `size_matched`; `trade` events carry our maker orders' matched
// amounts. `done` once the order no longer rests.
struct OrderUpdate {
  std::string id;
  double matched = 0;
  bool has_matched = false;
  bool done = false;
};
void parse_user(const json::Value& root, std::vector<OrderUpdate>& out);

// --- Spot (Coinbase) -------------------------------------------------------------

// {"type":"subscribe","product_ids":["BTC-USD",…],"channels":["ticker"]} for the assets.
std::string spot_subscribe(const std::vector<std::string>& assets);

// A Coinbase `ticker` frame (best bid/ask on each trade) into the coin's spot
// mid (`price` when either side is missing). The coin updated, or -1.
int apply_spot(const json::Value& root, engine::Engine& engine, std::int64_t recv_ns);

// RFC 3339 → unix seconds ("2026-10-26T12:00:00Z", fractional seconds and
// offsets allowed; a space may stand for the T).
bool iso_to_unix(std::string_view text, std::int64_t& out) noexcept;

}  // namespace pkm::feeds
