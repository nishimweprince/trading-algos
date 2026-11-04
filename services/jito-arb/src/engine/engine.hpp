#pragma once
// Decision engine (spec §6.2): on every pool update re-price only the cycles that touch it,
// size each, subtract every cost and hand the best to the dispatcher. One pinned thread.
#include <cstdint>
#include <functional>
#include <map>

#include "core/config.hpp"
#include "engine/costs.hpp"
#include "engine/dispatcher.hpp"
#include "engine/sizer.hpp"
#include "flash/kamino.hpp"
#include "state/state_store.hpp"

namespace arb::engine {

struct EngineConfig {
  uint64_t max_state_age_slots = 2;
  uint64_t max_slot_lag = 150;  // a leg whose snapshot is this far behind the slot clock waits for resync
  uint32_t min_edge_ppm = 500;
  uint32_t max_input_fraction_ppm = 200'000;
  uint64_t max_trade_lamports = 500'000'000'000ULL;
  uint64_t min_trade_lamports = 1'000'000;
  int sizer_max_evals = 24;
  bool flash = true;  // funding_mode == flash
  CostConfig costs;
  DispatchConfig dispatch;
};

EngineConfig engine_config(const Config& c);
TipPolicy::Params tip_params(const Config& c);

struct EngineCounters {
  uint64_t updates = 0, cycles_seen = 0, stale = 0, unsupported = 0, cooling = 0, prefilter_pass = 0, sized = 0, candidates = 0,
           sent = 0, skipped = 0;
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

  const EngineCounters& counters() const { return n_; }
  const CycleIndex& cycles() const { return idx_; }
  const BasePrice* base_price(const Pubkey& mint) const;
  const flash::FlashReserve* flash_reserve(const Pubkey& mint) const {
    auto it = reserves_.find(mint);
    return it == reserves_.end() ? nullptr : &it->second;
  }
  bool flash_mode() const { return cfg_.flash; }

 private:
  bool load(const Cycle& c, Snapshots& s, uint64_t slot) const;
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
};

}  // namespace arb::engine
