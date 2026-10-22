#pragma once

#include "hla/bridge_layout.hpp"
#include "hla/config.hpp"
#include "hla/hl_gateway.hpp"
#include "hla/hl_instruments.hpp"
#include "hla/hl_parser.hpp"
#include "hla/mt5_bridge.hpp"
#include "hla/types.hpp"

#include <array>
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace hla {

// What the engine needs from the outside world. Live: the HL gateway, the MT5 bridge, the
// journal and the kill file. Tests and drills: recorders.
class EngineIo {
 public:
  virtual ~EngineIo() = default;
  virtual bool hl_command(const HlCommand& command) = 0;
  virtual bool mt5_order(const bridge::OrderRequest& order) = 0;
  virtual void journal(std::string_view line) = 0;
  // Writes the durable kill file: a restart stays halted until a human removes it.
  virtual void kill(std::string_view reason) = 0;
};

struct PairSetup final {
  HlMarket hl;
  std::string mt5_symbol;
  double ratio{1.0};
  std::int64_t hl_maker_ppm{};
};

// HL account state from the aux thread (summed over the dexes in the basket).
struct AuxReport final {
  std::uint64_t receive_ns{};
  bool ok{};
  double hl_account_value{};
  double hl_min_withdrawable{};  // smallest per-dex withdrawable
  std::array<Decimal, kMaxSymbols> hl_positions{};
  bool has_rate_limit{};
  std::int64_t requests_remaining{};
};

enum class Block : std::uint8_t {
  none, no_spec, no_mt5_tick, mt5_stale, mt5_closed, mt5_spread, hl_stale, no_hl_book, basis_cold,
  rollover, mark_oracle, fair_gap, basis_large, size, count
};
enum class Halt : std::uint8_t {
  none, killed, emergency, bridge_stale, gateway_down, mt5_account, aux_stale, daily_loss, budget, margin,
  hedge_unknown, position_mismatch, forced_fill, count
};
std::string_view block_name(Block block) noexcept;
std::string_view halt_name(Halt halt) noexcept;

enum class QuoteState : std::uint8_t { idle, placing, resting, modifying, canceling };

struct Quote final {
  QuoteState state{QuoteState::idle};
  Price price{};
  Quantity size{};  // remaining
  std::uint64_t oid{};
  Cloid cloid{};
  Price pending_price{};
  Quantity pending_size{};
  Cloid pending_cloid{};
  std::uint64_t request_id{};
  std::uint64_t resting_since_ns{};
};

// Counters exposed in status JSON / journal for ops monitoring.
struct EngineMetrics final {
  std::uint64_t places_ok{};
  std::uint64_t places_reject{};
  std::uint64_t modifies{};
  std::uint64_t cancels{};
  std::uint64_t fills{};
  double fill_notional_usd{};
  double fill_fees_usd{};
  std::uint64_t soft_halt_events{};
  std::uint64_t pulls{};
};

class Engine final {
 public:
  // `simulated` (dry-run): fills and hedges are simulated, so venue positions are not reconciled.
  Engine(const Config& config, std::vector<PairSetup> pairs, EngineIo& io, bool simulated_mt5 = false);

  void on_hl_frame(const HlFrame& frame, std::uint64_t now_ns);
  void on_hl_event(const HlEvent& event, std::uint64_t now_ns);
  void on_mt5_tick(const bridge::Tick& tick, std::uint64_t now_ns);
  void on_mt5_txn(const bridge::Txn& txn, std::uint64_t now_ns);
  // Polled from the bridge every few ms: specs, account, positions, service heartbeats.
  void on_mt5_state(std::size_t symbol, const Mt5Spec& spec);
  void on_mt5_account(const Mt5Account& account, std::uint64_t publisher_ns, std::uint64_t executor_ns,
                      std::uint64_t now_ns);
  void on_aux(const AuxReport& report, std::uint64_t now_ns);
  void on_kill_file(bool present, std::uint64_t now_ns);
  // Timeouts, staleness, requote sweep and once-a-second journal snapshots.
  void on_timer(std::uint64_t now_ns);
  // Pull every quote (shutdown, fatal error). Quotes already in flight finish via acks.
  void pull_all(std::uint64_t now_ns, std::string_view reason);

  // Starting inventory (from HL clearinghouse and the MT5 positions report).
  void seed_positions(std::size_t symbol, Decimal hl_units, double mt5_lots);
  [[nodiscard]] double unhedged_lots(std::size_t symbol) const noexcept;

  [[nodiscard]] std::size_t symbols() const noexcept { return pairs_.size(); }
  [[nodiscard]] const PairSetup& pair(std::size_t symbol) const noexcept { return pairs_[symbol]; }
  [[nodiscard]] const Quote& quote(std::size_t symbol, Side side) const noexcept {
    return state_[symbol].quotes[side == Side::buy ? 0 : 1];
  }
  [[nodiscard]] Level hl_bid(std::size_t symbol) const noexcept { return state_[symbol].hl_bid; }
  [[nodiscard]] Level hl_ask(std::size_t symbol) const noexcept { return state_[symbol].hl_ask; }
  [[nodiscard]] double mt5_bid(std::size_t symbol) const noexcept { return state_[symbol].mt5_bid; }
  [[nodiscard]] double mt5_ask(std::size_t symbol) const noexcept { return state_[symbol].mt5_ask; }
  [[nodiscard]] Decimal hl_position(std::size_t symbol) const noexcept { return state_[symbol].hl_units; }
  [[nodiscard]] double mt5_position(std::size_t symbol) const noexcept { return state_[symbol].mt5_lots; }
  [[nodiscard]] bool hedge_in_flight(std::size_t symbol) const noexcept { return state_[symbol].hedge.request_id != 0; }
  [[nodiscard]] Block block(std::size_t symbol) const noexcept { return state_[symbol].block; }
  [[nodiscard]] bool halted() const noexcept { return halt_ != Halt::none; }
  [[nodiscard]] Halt halt() const noexcept { return halt_; }
  [[nodiscard]] bool failed() const noexcept { return failed_; }
  [[nodiscard]] double fair(std::size_t symbol) const noexcept;
  [[nodiscard]] const EngineMetrics& metrics() const noexcept { return metrics_; }
  [[nodiscard]] std::string status_json(std::uint64_t now_ns) const;

  // Pure quote math, exposed for tests.
  struct Desired final {
    bool active{};
    Price price{};
    Quantity size{};
  };
  [[nodiscard]] std::array<Desired, 2> desired_quotes(std::size_t symbol, std::uint64_t now_ns);

 private:
  struct Hedge final {
    std::uint64_t request_id{};
    double lots{};  // signed
    std::uint64_t sent_ns{};
    double position_before{};
    int attempts{};
  };
  struct Symbol final {
    Level hl_bid{};
    Level hl_ask{};
    std::uint64_t hl_bbo_ns{};
    std::uint64_t hl_ctx_ns{};
    Price mark{};
    Price oracle{};
    Decimal funding{};
    double mt5_bid{};
    double mt5_ask{};
    std::uint64_t mt5_tick_ns{};
    Mt5Spec spec{};
    double basis{};
    bool basis_seeded{};
    std::uint64_t basis_start_ns{};
    std::uint64_t basis_last_ns{};
    std::array<Quote, 2> quotes{};
    std::uint64_t last_requote_ns{};
    Decimal hl_units{};
    double mt5_lots{};
    Hedge hedge{};
    std::uint64_t first_unhedged_ns{};
    bool flattened{};
    Block block{Block::no_spec};
    std::uint64_t mt5_mismatch_since_ns{};
    std::uint64_t hl_mismatch_since_ns{};
    std::uint64_t last_hedge_ns{};
    std::uint64_t next_place_ns{};   // backoff after a rejected place
    std::uint64_t place_backoff_ms{};
    double half_spread_ppm{};
    std::uint64_t fills{};
    std::uint64_t places_reject{};
  };
  struct Flight final {
    std::uint64_t request_id{};
    HlCommandKind kind{};
    std::uint16_t symbol{};
    std::uint8_t count{};
    std::array<std::uint8_t, kMaxBatch> sides{};  // 0 bid, 1 ask, 2 flatten
  };

  void update_basis(std::size_t symbol, std::uint64_t now_ns);
  void requote(std::size_t symbol, std::uint64_t now_ns);
  void cancel_side(std::size_t symbol, std::size_t side, std::uint64_t now_ns);
  void hedge(std::size_t symbol, std::uint64_t now_ns);
  void hedge_result(std::size_t symbol, double filled_lots, std::uint64_t now_ns);
  void emergency(std::size_t symbol, std::string_view reason, std::uint64_t now_ns);
  void set_halt(Halt halt, std::string_view detail, std::uint64_t now_ns);
  void check_global(std::uint64_t now_ns);
  Block evaluate(std::size_t symbol, std::uint64_t now_ns) const;
  [[nodiscard]] double units_per_lot_step(std::size_t symbol) const noexcept;
  [[nodiscard]] double inventory_steps(std::size_t symbol) const noexcept;
  bool send(HlCommand& command, std::uint16_t symbol, std::span<const std::uint8_t> sides);
  Cloid next_cloid() noexcept;
  void journal(std::string_view line);
  void journal_snapshot(std::uint64_t now_ns);
  void on_ack(const HlEvent& event, std::uint64_t now_ns);
  void on_fill(const HlEvent& event, std::uint64_t now_ns);

  const Config& config_;
  std::vector<PairSetup> pairs_;
  EngineIo& io_;
  bool simulated_mt5_;
  std::vector<Symbol> state_;
  std::vector<Flight> flights_;
  Halt halt_{Halt::none};
  Halt soft_halt_{Halt::none};  // clears itself when the condition clears
  bool killed_{};
  bool failed_{};
  bool gateway_up_{};
  Mt5Account account_{};
  std::uint64_t publisher_ns_{};
  std::uint64_t executor_ns_{};
  AuxReport aux_{};
  std::uint64_t requests_sent_{};
  std::uint64_t requests_at_aux_{};
  double day_start_equity_{};
  std::uint64_t day_start_ns_{};
  std::uint64_t next_request_id_{1};
  std::uint64_t cloid_counter_{};
  std::uint64_t cloid_session_{};
  std::uint64_t last_snapshot_ns_{};
  std::uint64_t last_soft_halt_journal_ns_{};  // rate-limit soft_halt / pull spam when flapping
  std::uint64_t bridge_stale_since_ns_{};      // hysteresis before soft_halt=bridge_stale
  EngineMetrics metrics_{};
  std::array<char, 1536> line_{};
};

}  // namespace hla
