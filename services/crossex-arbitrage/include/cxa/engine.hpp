#pragma once

#include "cxa/book.hpp"
#include "cxa/config.hpp"
#include "cxa/instruments.hpp"
#include "cxa/latency.hpp"
#include "cxa/parser.hpp"

#include <array>
#include <cstdint>
#include <span>
#include <vector>

namespace cxa {

// Direction +1 (A): long Binance, short the peer. Direction -1 (B): long the peer, short Binance.
enum class Direction : std::int8_t { long_binance = 1, long_peer = -1 };
inline constexpr std::int64_t sign(Direction dir) noexcept { return static_cast<std::int64_t>(dir); }
inline constexpr Venue long_venue(Direction dir, Venue peer = Venue::bybit) noexcept {
  return dir == Direction::long_binance ? Venue::binance : peer;
}
inline constexpr Venue short_venue(Direction dir, Venue peer = Venue::bybit) noexcept {
  return dir == Direction::long_binance ? peer : Venue::binance;
}

enum class LegPurpose : std::uint8_t { open, close, repair };
enum class LegLiquidity : std::uint8_t { taker, maker };
enum class PairPhase : std::uint8_t { flat, opening, open, closing };
enum class ExitReason : std::uint8_t {
  none, converged, max_hold, funding, stop, kill, shutdown, legged_out, missed
};
enum class RiskCode : std::uint8_t {
  ok, halted, symbol_disabled, feed_stale, book_stale, fees_unknown, funding_unknown, basis_cold,
  max_pairs, notional_limit, cooldown, below_minimum, count
};

std::string_view exit_reason_name(ExitReason reason) noexcept;
std::string_view risk_code_name(RiskCode code) noexcept;

struct LegRecord final {
  std::uint64_t leg_id{};
  std::uint64_t cycle_id{};
  std::uint16_t symbol{};
  Venue venue{Venue::binance};
  Side side{Side::buy};
  LegPurpose purpose{LegPurpose::open};
  LegLiquidity liquidity{LegLiquidity::taker};
  Quantity quantity{};
  Price limit{};
  std::uint64_t decision_ns{};
  std::uint64_t arrival_ns{};
  // Set when the simulated IOC resolves, or on each maker touch fill.
  Quantity filled{};
  Price vwap{};
  std::int64_t fee_raw{};
  std::uint64_t fill_ns{};
  Venue hedge_venue{Venue::binance};
  Side hedge_side{Side::buy};
};

struct CycleRecord final {
  std::uint64_t cycle_id{};
  std::uint16_t symbol{};
  Direction dir{Direction::long_binance};
  ExitReason reason{ExitReason::none};
  std::uint64_t entry_ns{};
  std::uint64_t exit_ns{};
  Quantity quantity{};
  std::int64_t entry_gap_ppm{};
  std::int64_t exit_gap_ppm{};
  std::int64_t expected_capture_ppm{};
  std::int64_t expected_net_ppm{};
  std::int64_t gross_raw{};    // realised price P&L across both legs
  std::int64_t fees_raw{};
  std::int64_t funding_raw{};  // received positive
  std::int64_t net_raw{};
  std::int64_t notional_raw{};
  std::uint32_t repairs{};
};

struct DecisionRecord final {
  std::uint64_t cycle_id{};
  std::uint16_t symbol{};
  Direction dir{Direction::long_binance};
  std::uint64_t decision_ns{};
  Quantity quantity{};
  std::int64_t gap_ppm{};
  std::int64_t exit_gap_ppm{};
  std::int64_t basis_ppm{};
  std::int64_t mean_ppm{};
  std::int64_t capture_ppm{};
  std::int64_t fees_ppm{};
  std::int64_t funding_ppm{};
  std::int64_t net_ppm{};
};

struct CensusRecord final {
  std::uint16_t symbol{};
  std::uint8_t size_index{};
  Direction dir{Direction::long_binance};
  std::int64_t threshold_ppm{};
  std::uint64_t start_ns{};
  std::uint64_t duration_ns{};
  std::int64_t peak_ppm{};
};

enum class OutputKind : std::uint8_t { leg_submit, leg_fill, cycle, decision, census, resync, disabled };
struct Output final {
  OutputKind kind{OutputKind::leg_submit};
  LegRecord leg{};
  CycleRecord cycle{};
  DecisionRecord decision{};
  CensusRecord census{};
  Venue venue{Venue::binance};  // resync / disabled
  std::uint16_t symbol{};
};

struct Edge final {
  bool valid{};
  Quantity quantity{};
  SweepResult buy{};        // entry buy on the long venue
  SweepResult sell{};       // entry sell on the short venue
  SweepResult exit_sell{};  // close the long
  SweepResult exit_buy{};   // close the short
  std::int64_t gap_ppm{};
  std::int64_t exit_gap_ppm{};
  std::int64_t basis_ppm{};  // direction-signed mid basis
  std::int64_t mean_ppm{};   // direction-signed EWMA basis
  std::int64_t capture_ppm{};
  std::int64_t fees_ppm{};
  std::int64_t funding_ppm{};
  std::int64_t net_ppm{};
  Venue maker_venue{Venue::binance};
};

struct PairState final {
  PairPhase phase{PairPhase::flat};
  Direction dir{Direction::long_binance};
  std::uint64_t cycle_id{};
  std::array<std::int64_t, kVenues> qty_raw{};  // signed
  std::array<std::int64_t, kVenues> avg_raw{};
  std::int64_t realized_raw{};
  std::int64_t fees_raw{};
  std::int64_t funding_raw{};
  std::uint32_t pending_legs{};
  std::uint32_t repairs{};
  std::uint64_t entry_ns{};
  std::int64_t entry_gap_ppm{};
  std::int64_t entry_mean_ppm{};
  std::int64_t expected_capture_ppm{};
  std::int64_t expected_net_ppm{};
  std::int64_t entry_notional_raw{};
  std::int64_t last_exit_gap_ppm{};
  ExitReason reason{ExitReason::none};
  std::uint64_t cooldown_until_ns{};
  bool opened{};  // at least one entry leg filled this cycle
  Venue maker_venue{Venue::binance};
};

struct FundingState final {
  Decimal rate{};
  std::uint64_t next_ms{};
  Price mark{};
  bool known{};
};

struct EngineMetrics final {
  std::array<std::uint64_t, kVenues> depth_events{};
  std::array<std::uint64_t, kVenues> gaps{};
  std::uint64_t funding_events{};
  std::uint64_t funding_settlements{};
  std::uint64_t legs{};
  std::uint64_t fills{};
  std::uint64_t cycles{};
  std::uint64_t missed{};
  std::uint64_t repairs{};
  std::uint64_t census_episodes{};
  std::array<std::uint64_t, static_cast<std::size_t>(RiskCode::count)> risk{};
  std::int64_t net_raw{};
  std::int64_t fees_raw{};
  std::int64_t funding_raw{};
  std::uint32_t open_pairs{};
  bool output_overflow{};
};

class Engine final {
 public:
  static constexpr std::size_t kMaxOutputs = 160;
  static constexpr std::size_t kMaxLegs = 64;

  Engine(std::vector<Instrument> instruments, StrategyConfig strategy = {}, RiskLimits risk = {},
         ShadowConfig shadow = {});

  void on_depth(const DepthEvent& event) noexcept;
  bool on_snapshot(const BookSnapshot& snapshot) noexcept;
  // A frame that could not be applied (e.g. a diff larger than kMaxDepthChanges).
  void on_feed_gap(Venue venue, std::uint16_t symbol, std::uint64_t receive_ns) noexcept;
  void on_funding(const FundingEvent& event) noexcept;
  void set_fees(Venue venue, std::uint16_t symbol, std::int64_t taker_ppm, std::int64_t maker_ppm) noexcept;
  void set_taker_fee(Venue venue, std::uint16_t symbol, std::int64_t taker_ppm) noexcept;
  void add_latency_sample(Venue venue, std::uint64_t rtt_ns) noexcept;
  // Durable halt: flattens open pairs through the normal (latency-delayed) path.
  void kill(std::uint64_t now_ns) noexcept;
  void acknowledge_halt() noexcept;
  // Immediate mark-out at current books, used at process stop.
  void liquidate(std::uint64_t now_ns, ExitReason reason) noexcept;

  [[nodiscard]] std::span<const Output> outputs() const noexcept { return {outputs_.data(), output_count_}; }
  void clear_outputs() noexcept { output_count_ = 0; }

  [[nodiscard]] const std::vector<Instrument>& instruments() const noexcept { return instruments_; }
  [[nodiscard]] const OrderBook& book(Venue venue, std::size_t symbol) const noexcept {
    return books_[index(venue)][symbol];
  }
  [[nodiscard]] const PairState& pair(std::size_t symbol) const noexcept { return pairs_[symbol]; }
  [[nodiscard]] const FundingState& funding(Venue venue, std::size_t symbol) const noexcept {
    return funding_[index(venue)][symbol];
  }
  [[nodiscard]] std::int64_t taker_ppm(Venue venue, std::size_t symbol) const noexcept {
    return taker_ppm_[index(venue)][symbol];
  }
  [[nodiscard]] std::int64_t maker_ppm(Venue venue, std::size_t symbol) const noexcept {
    return maker_ppm_[index(venue)][symbol];
  }
  [[nodiscard]] std::int64_t basis_mean_ppm(std::size_t symbol) const noexcept;
  [[nodiscard]] bool basis_warm(std::size_t symbol) const noexcept;
  [[nodiscard]] bool symbol_disabled(std::size_t symbol) const noexcept { return disabled_[symbol]; }
  [[nodiscard]] const EngineMetrics& metrics() const noexcept { return metrics_; }
  [[nodiscard]] const LatencyModel& latency(Venue venue) const noexcept { return latency_[index(venue)]; }
  [[nodiscard]] bool halted() const noexcept { return halted_ || loss_halt_; }
  [[nodiscard]] bool loss_halted() const noexcept { return loss_halt_; }
  [[nodiscard]] std::uint64_t now_ns() const noexcept { return now_ns_; }
  [[nodiscard]] Venue peer() const noexcept { return shadow_.peer; }
  [[nodiscard]] std::uint64_t state_hash() const noexcept;
  [[nodiscard]] Edge edge(std::size_t symbol, Direction dir, Quantity quantity) const noexcept;
  [[nodiscard]] Edge edge(std::size_t symbol, Direction dir, Quantity quantity, Venue maker_venue) const noexcept;
  [[nodiscard]] RiskCode entry_risk(std::size_t symbol, std::int64_t notional_raw) const noexcept;

 private:
  struct PendingLeg final {
    bool active{};
    bool resting{};  // maker has arrived and waits for a trade-through
    LegRecord record{};
    Quantity remaining{};
    std::uint64_t expire_ns{};
  };
  struct Basis final {
    double mean_ppm{};
    std::uint64_t first_ms{};
    std::uint64_t last_ms{};
    bool initialised{};
  };
  struct Episode final {
    bool active{};
    std::uint64_t start_ns{};
    std::int64_t peak_ppm{};
  };

  void advance(std::uint64_t now_ns) noexcept;
  void process_arrivals() noexcept;
  void process_resting_makers() noexcept;
  void resolve_pair(std::size_t symbol) noexcept;
  void evaluate(std::size_t symbol) noexcept;
  void update_basis(std::size_t symbol) noexcept;
  void update_census(std::size_t symbol) noexcept;
  void close_episodes(std::size_t symbol) noexcept;
  void try_open(std::size_t symbol) noexcept;
  void check_exit(std::size_t symbol) noexcept;
  void begin_close(std::size_t symbol, ExitReason reason) noexcept;
  void submit_leg(std::size_t symbol, Venue venue, Side side, LegPurpose purpose, Quantity quantity,
                  Price limit, LegLiquidity liquidity = LegLiquidity::taker, Venue hedge_venue = Venue::binance,
                  Side hedge_side = Side::buy) noexcept;
  [[nodiscard]] Venue long_of(Direction dir) const noexcept { return long_venue(dir, shadow_.peer); }
  [[nodiscard]] Venue short_of(Direction dir) const noexcept { return short_venue(dir, shadow_.peer); }
  [[nodiscard]] Venue counterpart(Venue venue) const noexcept {
    return venue == Venue::binance ? shadow_.peer : Venue::binance;
  }
  void apply_fill(PairState& pair, Venue venue, Side side, Quantity quantity, Price price) noexcept;
  void finish_cycle(std::size_t symbol, ExitReason reason) noexcept;
  void on_gap(Venue venue, std::size_t symbol) noexcept;
  void mark_out(std::size_t symbol, ExitReason reason) noexcept;
  void cancel_resting_maker(std::size_t symbol) noexcept;
  void on_maker_fill(PendingLeg& slot, Quantity filled, Price price) noexcept;
  Output* push_output(OutputKind kind) noexcept;
  [[nodiscard]] Quantity quantity_for(std::size_t symbol, std::int64_t notional_raw) const noexcept;
  [[nodiscard]] std::int64_t funding_cost_ppm(std::size_t symbol, Direction dir,
                                              std::uint64_t horizon_ms) const noexcept;
  [[nodiscard]] std::int64_t mid_basis_ppm(std::size_t symbol) const noexcept;
  [[nodiscard]] std::int64_t cycle_fees_ppm(std::size_t symbol, Venue maker_venue) const noexcept;
  [[nodiscard]] Edge edge_taker_taker(std::size_t symbol, Direction dir, Quantity quantity) const noexcept;
  [[nodiscard]] Edge edge_maker_taker(std::size_t symbol, Direction dir, Quantity quantity,
                                      Venue maker_venue) const noexcept;

  std::vector<Instrument> instruments_;
  StrategyConfig strategy_;
  RiskLimits limits_;
  ShadowConfig shadow_;
  std::array<std::array<OrderBook, kMaxSymbols>, kVenues> books_{};
  std::array<std::array<FundingState, kMaxSymbols>, kVenues> funding_{};
  std::array<std::array<std::int64_t, kMaxSymbols>, kVenues> taker_ppm_{};
  std::array<std::array<std::int64_t, kMaxSymbols>, kVenues> maker_ppm_{};
  std::array<LatencyModel, kVenues> latency_{};
  std::array<std::uint64_t, kVenues> last_receive_ns_{};
  std::array<PairState, kMaxSymbols> pairs_{};
  std::array<Basis, kMaxSymbols> basis_{};
  std::array<bool, kMaxSymbols> disabled_{};
  std::array<bool, kMaxSymbols> mid_checked_{};
  std::array<std::array<std::array<std::array<Episode, kCensusThresholds>, 2>, kCensusSizes>, kMaxSymbols>
      episodes_{};
  std::array<PendingLeg, kMaxLegs> legs_{};
  std::array<Output, kMaxOutputs> outputs_{};
  std::size_t output_count_{};
  EngineMetrics metrics_{};
  std::uint64_t now_ns_{};
  std::uint64_t next_leg_id_{1};
  std::uint64_t next_cycle_id_{1};
  bool halted_{};     // kill file; cleared when the file is removed
  bool loss_halt_{};  // daily loss or failed repair; cleared only by restart
};

}  // namespace cxa
