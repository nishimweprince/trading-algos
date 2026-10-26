#pragma once

#include <array>
#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "pkm/book.hpp"

// The maker's rules, ported one for one from pk-polymaker's `engine.rs`: two
// resting post-only buys per coin on Polymarket's 15-minute Up/Down market,
// priced off Kalshi's YES mid for the same window. No I/O: the runtime feeds it
// book updates and venue answers, and executes the actions it emits.
//
// One change from the Rust maker: quotes are re-evaluated on every book update
// of a coin, not on a 50 ms timer; `tick` (every 10 ms) drives the hold rules
// and the window edges.
namespace pkm::engine {

inline constexpr std::int64_t kWindowS = 900;
inline constexpr double kTick = 0.01;

struct Config {
  double half_spread = 0.02;
  double shares = 5;
  double min_shares = 5;
  double max_net = 20;
  double skew_k = 0.01;
  double pause_jump = 0.02;
  std::int64_t pause_ms = 1500;
  double max_kalshi_spread = 0.02;
  std::int64_t kalshi_max_age_ms = 10'000;
  double min_px = 0.05;
  double max_px = 0.95;
  std::int64_t entry_early_s = 20;
  std::int64_t stop_s = 60;
  double requote_cents = 1;
  std::int64_t min_requote_ms = 300;
  std::int64_t adverse_ms = 200;
  std::int64_t reject_cooldown_ms = 500;
  double max_orders_per_s = 8;
  double daily_loss_usd = 15;
  double max_open_cost_usd = 60;
  double pair_margin = 0.02;
  std::int64_t open_leg_s = 300;
  double open_min_fair = 0.15;
  double open_max_fair = 0.85;
};

struct Window {
  std::string asset;
  std::int64_t open_unix = 0;
  std::string kalshi_ticker;
  std::string up_token;    // decimal token id
  std::string down_token;
  std::string condition_id;
  bool neg_risk = false;
  bool operator==(const Window&) const = default;
};

std::string window_key(std::string_view asset, std::int64_t open_unix);
std::string utc_day(std::int64_t t_ns);

enum class ActionKind : std::uint8_t { post, cancel, cancel_all };
struct Action {
  ActionKind kind = ActionKind::post;
  int coin = -1;
  bool up = true;
  double px = 0;      // post: price
  double size = 0;    // post: shares
  std::string order_id;  // cancel
  std::string reason;    // cancel_all
};

// pure helpers (engine.rs)
double floor_tick(double x) noexcept;
std::pair<std::optional<double>, std::optional<double>> desired_px(const Config& cfg, double q, double net,
                                                                   std::optional<double> up_ask,
                                                                   std::optional<double> down_ask) noexcept;
bool opens(double net, bool up) noexcept;
bool first_leg_ok(const Config& cfg, std::int64_t s, double q) noexcept;
std::optional<double> pair_cap(const Config& cfg, std::optional<double> px, std::optional<double> opposite_avg) noexcept;
std::optional<double> room(const Config& cfg, double net, bool up) noexcept;

struct Inventory {
  std::string asset;
  std::int64_t open_unix = 0;
  std::string condition_id;
  std::string up_token;
  std::string down_token;
  double up = 0;
  double up_cost = 0;
  double down = 0;
  double down_cost = 0;
  std::uint64_t fills = 0;
  std::string settled;  // "", "yes", "no"
  double pnl = 0;
  std::string redeemed;  // tx hash once redeemed
  std::uint32_t redeem_tries = 0;
  [[nodiscard]] double net() const noexcept { return up - down; }
  [[nodiscard]] double cost() const noexcept { return up_cost + down_cost; }
  [[nodiscard]] double settle_pnl(std::string_view outcome) const noexcept {
    return (outcome == "yes" ? up : down) - cost();
  }
  [[nodiscard]] std::int64_t close_ns() const noexcept { return (open_unix + kWindowS) * 1'000'000'000LL; }
};

struct OrderRec {
  std::string id;
  std::string key;  // window key
  int coin = -1;
  bool up = true;
  double px = 0;
  double size = 0;
  double matched = 0;
  std::int64_t posted_ns = 0;
  bool closing = false;  // cancelled: no fills after the final state
};

struct Halt {
  std::string day;
  std::string reason;
  std::int64_t t_ns = 0;
};

struct Gates {
  bool kill = false;
  bool feeds_ok = false;
};

struct Counters {
  std::uint64_t posts = 0, cancels = 0, post_only_rejects = 0, balance_rejects = 0, post_errors = 0,
                cancel_errors = 0, rate_limited = 0, cash_waits = 0, pauses = 0, fills = 0, orders_unknown = 0,
                windows = 0, drift_checks = 0;
};

class Engine final {
 public:
  Engine(Config cfg, std::vector<std::string> assets);

  [[nodiscard]] const Config& config() const noexcept { return cfg_; }
  [[nodiscard]] std::size_t coins() const noexcept { return coins_.size(); }
  [[nodiscard]] const std::string& asset(int coin) const { return coins_[static_cast<std::size_t>(coin)].name; }
  [[nodiscard]] int coin_of(std::string_view asset) const noexcept;
  [[nodiscard]] const Window* window(int coin) const noexcept;
  void set_window(const Window& w, std::int64_t t_ns);

  // Books, for the feed decoders.
  [[nodiscard]] int coin_for_ticker(std::string_view ticker) const noexcept;
  // Token → (coin, up); coin -1 when not ours.
  [[nodiscard]] std::pair<int, bool> coin_for_token(std::string_view token) const noexcept;
  Book& kalshi_book(int coin) noexcept { return coins_[static_cast<std::size_t>(coin)].kbook; }
  Book& poly_book(int coin, bool up) noexcept {
    auto& c = coins_[static_cast<std::size_t>(coin)];
    return up ? c.upb : c.downb;
  }
  void invalidate_kalshi() noexcept;
  void invalidate_poly(int coin) noexcept;  // -1: all

  // After a coin's Kalshi book changed: the pause trail, then quotes.
  void on_kalshi_update(int coin, std::int64_t t_ns);
  // After a coin's Polymarket book changed.
  void on_poly_update(int coin, std::int64_t t_ns);
  void set_gates(const Gates& gates) noexcept { gates_ = gates; }
  void tick(std::int64_t t_ns);
  void stop() noexcept { stopped_ = true; }

  // Actions since the last call (moved out; the vector keeps its capacity).
  std::vector<Action>& actions() noexcept { return actions_; }

  // Venue answers.
  void on_posted(int coin, bool up, double px, double size, bool ok, std::string_view id_or_error, std::int64_t t_ns);
  void on_cancelled(int coin, bool up, std::string_view order_id, bool ok, std::int64_t t_ns);
  // Total matched shares; `done` once the order no longer rests.
  void on_order(std::string_view id, double matched, bool done, std::int64_t t_ns);
  void on_unknown(std::string_view id, std::int64_t t_ns);
  [[nodiscard]] std::vector<std::string> tracked() const;

  // Settlement, redemption, reconciliation.
  [[nodiscard]] std::vector<Inventory> unsettled(std::int64_t t_ns) const;
  void settle(std::string_view asset, std::int64_t open_unix, std::string_view outcome, std::int64_t t_ns);
  [[nodiscard]] std::vector<std::pair<std::string, std::string>> redeem_due() const;
  void on_redeemed(std::string_view key, bool ok, std::string_view tx_or_error, std::int64_t t_ns);
  [[nodiscard]] std::map<std::string, double> held_tokens(std::int64_t t_ns) const;
  void on_balance(std::string_view token, double wallet, std::int64_t t_ns);

  // Risk.
  [[nodiscard]] bool halted(std::int64_t t_ns) const;
  void halt(std::string_view reason, std::int64_t t_ns);
  void ack(std::int64_t t_ns);
  [[nodiscard]] double today_realised(std::int64_t t_ns) const;
  [[nodiscard]] double open_cost() const;
  [[nodiscard]] const std::optional<Halt>& halt_state() const noexcept { return halt_; }

  // Persistence: inventories, orders, realised by day, halt, ack.
  [[nodiscard]] std::string save_json() const;
  bool load_json(std::string_view text, std::string& error);
  // Set when state worth persisting changed; the runtime clears it after saving.
  bool dirty = false;

  // Events worth a log line (halts, rejects, fills, settles), drained by the runtime.
  std::vector<std::string>& log() noexcept { return log_; }
  [[nodiscard]] const Counters& counters() const noexcept { return counters_; }
  [[nodiscard]] const char* gate(int coin) const noexcept { return coins_[static_cast<std::size_t>(coin)].gate; }
  [[nodiscard]] const std::map<std::string, Inventory, std::less<>>& inventories() const noexcept { return inv_; }
  [[nodiscard]] const std::map<std::string, OrderRec, std::less<>>& orders() const noexcept { return orders_; }
  // Seeds a window's inventory (tests, and importing pk-polymaker's state).
  void put_inventory(const std::string& key, const Inventory& v) {
    inv_[key] = v;
    dirty = true;
  }
  // Resting bid of a slot: (order id, px), or none.
  [[nodiscard]] std::optional<std::pair<std::string, double>> resting(int coin, bool up) const;
  // The price each slot wants now, or will want once a pause or the window
  // start ends (for the pre-signer); none otherwise.
  [[nodiscard]] std::optional<std::pair<double, double>> target(int coin, bool up) const noexcept;

 private:
  struct Slot {
    std::string order;  // empty = none
    double px = 0;
    double remaining = 0;
    bool inflight = false;
    std::int64_t last_change_ms = 0;
    std::int64_t cooldown_until_ms = 0;
    // Since when the target has been above (+1) or below (−1) the resting bid.
    std::int8_t off_dir = 0;
    std::int64_t off_since_ms = 0;
    bool has_off = false;
    std::optional<std::pair<double, double>> want;
    std::optional<std::pair<double, double>> hint;
  };
  struct Coin {
    std::string name;
    std::optional<Window> window;
    std::string key;
    Book kbook;
    Book upb;
    Book downb;
    std::deque<std::pair<std::int64_t, double>> trail;
    std::int64_t pause_until_ms = 0;
    std::array<Slot, 2> slots;
    const char* gate = "no window";
  };

  [[nodiscard]] std::optional<double> fair(const Coin& c, std::int64_t now_ms) const noexcept;
  void tick_coin(std::size_t i, std::int64_t t_ns);
  void step_slot(std::size_t i, std::size_t n, std::optional<std::pair<double, double>> want, std::int64_t now_ms);
  bool take_token(std::int64_t now_ms) noexcept;
  [[nodiscard]] double net_of(std::string_view key) const noexcept;
  Slot* slot(int coin, bool up) noexcept;
  void book_fill(const OrderRec& o, double shares, std::int64_t t_ns);
  void note(std::string line);

  Config cfg_;
  std::vector<Coin> coins_;
  std::map<std::string, std::pair<int, bool>, std::less<>> owner_;
  std::map<std::string, Inventory, std::less<>> inv_;
  std::map<std::string, OrderRec, std::less<>> orders_;
  std::map<std::string, double, std::less<>> realised_by_day_;
  std::optional<Halt> halt_;
  std::optional<std::pair<std::string, double>> ack_;
  std::vector<Action> actions_;
  std::vector<std::string> log_;
  double bucket_;
  std::int64_t bucket_ms_ = 0;
  bool stopped_ = false;
  Gates gates_;
  Counters counters_;
  std::map<std::string, std::pair<std::uint32_t, double>, std::less<>> drift_;
  // Free balance from a "not enough balance" refusal, and until when.
  std::optional<std::pair<double, std::int64_t>> cash_hint_;
};

}  // namespace pkm::engine
