#pragma once
// Decision engine (spec §6.2): on every pool update re-price only the cycles that touch it,
// size each, subtract every cost and hand the best to the dispatcher. One pinned thread.
#include <cstdint>
#include <functional>
#include <map>
#include <unordered_map>

#include "core/config.hpp"
#include "engine/costs.hpp"
#include "engine/dispatcher.hpp"
#include "engine/sizer.hpp"
#include "flash/kamino.hpp"
#include "obs/latency.hpp"
#include "state/state_store.hpp"

namespace arb::engine {

struct EngineConfig {
  uint64_t max_state_age_slots = 2;
  uint64_t settle_slots = 2;     // a component spread is fine once the newest write is this many slots old
  uint64_t max_slot_lag = 150;  // a leg whose snapshot is this far behind the slot clock waits for resync
  uint32_t min_edge_ppm = 500;
  uint32_t max_input_fraction_ppm = 200'000;
  uint64_t max_trade_lamports = 500'000'000'000ULL;
  uint64_t min_trade_lamports = 1'000'000;
  int sizer_max_evals = 24;
  int max_sized_per_update = 4;  // cycles sized per pool update, largest spot edge first (0: all)
  uint32_t max_profit_ppm = 20'000;  // engine.max_profit_ratio
  int quarantine_strikes = 3;
  int64_t quarantine_window_ms = 600'000, quarantine_ms = 1'800'000;
  bool flash = true;  // funding_mode == flash
  CostConfig costs;
  DispatchConfig dispatch;
};

EngineConfig engine_config(const Config& c);
TipPolicy::Params tip_params(const Config& c);

/** Why sized cycles failed (each sized-and-failed cycle counts once), plus a cross-cut. */
struct SizerDiag {
  uint64_t no_room = 0;       // x_max below min_trade (depth fraction, reserve liquidity or max_trade)
  uint64_t no_profit = 0;     // clean quotes, but profit <= 0 at every size tried: the spot edge was not real
  uint64_t partial_fill = 0;  // a leg could not take its whole input (Whirlpool price limit)
  uint64_t quote_error = 0;   // a quoter returned an error at every size
  uint64_t zero_out = 0;
  uint64_t boundary = 0;      // every size needed a tick array outside the cache or the 3-array window
  uint64_t boundary_cache = 0;  // ... of which at least one size hit an array the bot does not hold
  uint64_t other = 0;
  uint64_t zero_liquidity_leg = 0;  // a Whirlpool leg with zero active liquidity (spot price is not tradable)
  uint64_t quote_error_by_code[32]{};
};

/** The largest predicted edge that sized to no profit since the last take(): for the log. */
struct SizerExample {
  bool set = false;
  uint32_t cycle = 0;
  uint64_t slot = 0;
  int64_t edge_ppm = 0;      // spot-product edge the prefilter saw
  uint64_t x_max = 0, best_x = 0;
  int64_t best_profit = 0;   // base units, <= 0
  Reason reason = Reason::none;
  uint64_t min_trade = 0;
  Errc err = Errc::bad_data;  // last quote error (sizer_quote_error)
  int8_t fail_leg = -1;
};

struct EngineCounters {
  uint64_t untradable = 0;  // a leg whose pool the program rejects in that direction (until its state changes)
  uint64_t no_room_memo = 0;
  uint64_t quarantined = 0, implausible = 0;  // evaluations skipped: a quarantined leg / a too-large profit  // skipped: x_max was below min_trade and no leg or reserve changed since
  uint64_t updates = 0, cycles_seen = 0, stale = 0, unsupported = 0, cooling = 0, prefilter_pass = 0, sized = 0, candidates = 0,
           sent = 0, skipped = 0;
  uint64_t deferred = 0;  // passed the prefilter but not sized: below the update's top max_sized_per_update
  // `stale` by cause (first failing leg): a component not yet seen, a resync read awaiting its push, the pool
  // account and vaults written more than max_state_age_slots apart, or the snapshot behind the slot clock.
  uint64_t stale_incomplete = 0, stale_resync = 0, stale_spread = 0, stale_lag = 0;
  // Shadow pricing of cycles stale only by spread (never sent): how many cleared the spot-edge prefilter, how
  // many sized and priced as a candidate, and the sum of their gross. Measures what the spread rule hides.
  uint64_t spread_edge = 0, spread_priced = 0, spread_gross_lamports = 0;
};

/** Why a cycle's legs are not fresh (EngineCounters::stale_*). */
enum class StaleCause : uint8_t { fresh = 0, incomplete, resync, spread, lag };

/** A pool whose spread-only staleness hid priced candidates, for the log: the worst offenders since the last take. */
struct SpreadExample {
  uint32_t pool = 0;
  uint64_t edge_hits = 0, priced = 0;  // spread-stale evaluations with this pool that cleared the prefilter / priced
  int64_t best_net = 0;               // lamports, best priced candidate it blocked
  uint32_t best_cycle = 0;
};

class Engine {
 public:
  using SendFn = std::function<void(const Opportunity&)>;
  using LogFn = std::function<void(const Opportunity&, Reason)>;  // every sized candidate not sent

  Engine(const StateStore& store, const CycleIndex& idx, Quoters q, EngineConfig cfg, TipPolicy& tips);
  void on_send(SendFn f) { send_ = std::move(f); }
  void on_skip(LogFn f) { skip_ = std::move(f); }

  /** Base-token value in lamports (wSOL is 1.0 by default; USDC from the deepest SOL/USDC pool). */
  void set_base_price(const Pubkey& mint, BasePrice p) { prices_[mint] = p; }
  /** Price USDC (or any base) from the deepest tracked pool pairing it with wSOL. False if none is quotable. */
  bool refresh_base_price_from_pools(const Pubkey& base);
  void set_flash_reserve(const Pubkey& mint, const flash::FlashReserve& r) { reserves_[mint] = r; }
  /** Own-capital mode: wallet balance of the base token caps x_max. */
  void set_own_balance(const Pubkey& mint, uint64_t units) { own_balance_[mint] = units; }

  void on_pool_update(uint32_t pool, uint64_t slot);
  /** Dispatch the pass's candidates at the given slot clock. */
  void dispatch_best(uint64_t current_slot);
  /** Evaluate one cycle now (tests, replay tools). Returns false when it produced no candidate. */
  bool evaluate(uint32_t cycle, uint64_t slot, Opportunity* out = nullptr);
  /** Monotonic ns when the current pool update reached the engine (Runtime sets it): the prefilter timer's start. */
  void set_eval_start_ns(uint64_t t) { eval_start_ns_ = t; }
  /** Per pool update with a cycle past the prefilter: time in the prefilter stage, and in sizing and pricing, us. */
  obs::LatencyWindow& prefilter_us() { return prefilter_us_; }
  obs::LatencyWindow& size_us() { return size_us_; }

  const EngineCounters& counters() const { return n_; }
  const SizerDiag& sizer_diag() const { return diag_; }
  /** Predicted spot edge of every sized cycle, ppm (60 s window). */
  obs::LatencyWindow& sized_edge_ppm() { return edge_ppm_; }
  /** Best realized profit / size of cycles that sized to no profit, ppm (<= 0; 60 s window). */
  obs::LatencyWindow& failed_profit_ppm() { return failed_profit_ppm_; }
  /** One example per failure reason (the largest predicted edge since the last take), then cleared. */
  std::vector<SizerExample> take_examples() {
    std::vector<SizerExample> out;
    for (auto& e : examples_)
      if (e.set) out.push_back(e);
    for (auto& e : examples_) e = {};
    return out;
  }
  const CycleIndex& cycles() const { return idx_; }
  /**
   * A bundle simulation's verdict on one leg's pool direction: bad (the route failed at or after it, or came
   * out short) adds a strike; enough strikes in the window quarantine it. Good clears its strikes.
   */
  void note_sim(uint32_t pool, bool a_to_b, bool bad, int64_t now_ms);
  struct Quarantine {
    uint32_t pool = 0;
    bool a_to_b = false;
    int64_t until_ms = 0;
    uint32_t strikes = 0;
  };
  std::vector<Quarantine> quarantined(int64_t now_ms) const;
  const BasePrice* base_price(const Pubkey& mint) const;
  const flash::FlashReserve* flash_reserve(const Pubkey& mint) const {
    auto it = reserves_.find(mint);
    return it == reserves_.end() ? nullptr : &it->second;
  }
  bool flash_mode() const { return cfg_.flash; }
  /** Pools that blocked the most priced candidates by spread since the last call (at most `n`), then cleared. */
  std::vector<SpreadExample> take_spread_examples(size_t n);

 private:
  StaleCause load(const Cycle& c, Snapshots& s, uint64_t slot, uint64_t* spread_mask) const;
  /** x_max for a cycle (depth fraction, max_trade, flash reserve or own balance); false with `why` when none. */
  bool x_max_for(const Cycle& c, const Snapshots& s, const BasePrice& bp, uint64_t& x_max, Reason& why) const;
  void shadow_spread(uint32_t cycle, const Cycle& c, const Snapshots& s, uint64_t spread_mask);
  const StateStore& store_;
  const CycleIndex& idx_;
  Quoters q_;
  EngineConfig cfg_;
  TipPolicy& tips_;
  Dispatcher disp_;
  u128 edge_x64_;
  std::map<Pubkey, BasePrice> prices_;
  std::map<Pubkey, flash::FlashReserve> reserves_;
  std::map<Pubkey, uint64_t> own_balance_;
  SendFn send_;
  LogFn skip_;
  EngineCounters n_;
  SizerDiag diag_;
  obs::LatencyWindow edge_ppm_, failed_profit_ppm_, prefilter_us_, size_us_;
  std::vector<std::pair<u128, uint32_t>> passed_;  // on_pool_update scratch: (spot product, cycle)
  uint64_t eval_start_ns_ = 0;
  bool prefilter(uint32_t cycle, uint64_t slot, u128& prod);
  bool size_and_price(uint32_t cycle, uint64_t slot, u128 prod, Opportunity* out);
  SizerExample examples_[32];  // by Reason
  // (pool id << 1 | a_to_b) -> snapshot seq at which every size failed with a program error (e.g. Whirlpool
  // LiquidityUnderflow, which the chain returns too). Skipped until the pool's state changes.
  std::unordered_map<uint64_t, uint64_t> untradable_;
  std::unordered_map<uint32_t, SpreadExample> spread_by_pool_;
  struct NoRoom {
    uint64_t seq[MAX_LEGS]{};
    uint64_t min_trade = 0;
    bool reserve_bound = false;  // the reserve (not depth or max_trade) was below min_trade
  };
  std::unordered_map<uint32_t, NoRoom> no_room_;
  struct Strikes {
    std::vector<int64_t> at_ms;
    int64_t until_ms = 0;
  };
  std::unordered_map<uint64_t, Strikes> strikes_;
  int64_t eval_now_ms_ = 0;  // wall clock for the pool update being evaluated  // (pool << 1 | a_to_b)
  // cycle -> the inputs that gave x_max < min_trade
  uint64_t reserve_available(const Pubkey& base) const;
};

}  // namespace arb::engine
