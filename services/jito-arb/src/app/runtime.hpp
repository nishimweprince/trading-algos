#pragma once
// The hot path wired together, shared by the live loop, replay and the tests: frames -> FeedHandler ->
// StateStore -> Engine -> (send hook) and the decision log. Phase 2 runs it in replay only; phase 3
// plugs the transaction builder into the send hook (dry run unless the config says otherwise).
#include <functional>
#include <map>
#include <memory>
#include <string>

#include "app/pools_file.hpp"
#include "core/config.hpp"
#include "dex/quoter.hpp"
#include "engine/engine.hpp"
#include "feed/feed_handler.hpp"
#include "obs/decision_log.hpp"

namespace arb::app {

/** The decision-log record of one candidate (pools, legs, costs); action and reason are set by the caller. */
obs::DecisionRecord make_record(const StateStore& store, const engine::Cycle& c, const engine::Opportunity& o, int64_t wall_ms);

struct RuntimeStats {
  uint64_t frames = 0, bad_frames = 0, updates = 0, records = 0, sent = 0;
};

class Runtime {
 public:
  /** Called for each dispatched candidate; fills `rec.signature` / `rec.action` (dry run by default). */
  using SendHook = std::function<void(const engine::Opportunity&, const engine::Cycle&, obs::DecisionRecord& rec)>;

  Runtime(const Config& cfg, const PoolsFile& pools);
  void set_decision_log(obs::DecisionLog* log) { log_ = log; }
  void set_send_hook(SendHook h) { send_hook_ = std::move(h); }

  /** One raw SubscribeUpdate frame (live or replayed); drains changed pools through the engine. */
  void on_frame(const uint8_t* data, size_t len, uint64_t recv_ns, int64_t wall_ms);
  /** Replay a recorded frame file through the same path. False when the file cannot be opened. */
  bool replay_file(const std::string& path);

  StateStore& store() { return store_; }
  engine::Engine& engine() { return *engine_; }
  engine::TipPolicy& tips() { return tips_; }
  const engine::CycleIndex& cycles() const { return idx_; }
  feed::SlotClock& clock() { return clock_; }
  const RuntimeStats& stats() const { return stats_; }
  /** Receive time (monotonic ns, the stream's clock) of the newest account update; 0 before the first. */
  uint64_t last_update_ns() const { return handler_.last_update_ns(); }
  /** Receive time (monotonic ns) of the frame being processed: the start of detect-to-send. */
  uint64_t frame_recv_ns() const { return frame_recv_ns_; }
  feed::FeedHandler& feed() { return handler_; }

 private:
  void refresh_slot_inputs(uint64_t slot);
  const Config& cfg_;
  StateStore store_;
  engine::CycleIndex idx_;
  engine::TipPolicy tips_;
  dex::RaydiumV4Quoter ray_q_;
  dex::WhirlpoolQuoter wp_q_;
  dex::CpmmQuoter cpmm_q_;
  dex::PumpSwapQuoter pump_q_;
  dex::DlmmQuoter dlmm_q_;
  dex::ClmmQuoter clmm_q_;
  std::unique_ptr<engine::Engine> engine_;
  feed::SlotClock clock_;
  feed::FeedHandler handler_;
  std::vector<Pubkey> reserves_;
  std::map<Pubkey, flash::FlashReserve> reserve_cache_;  // static fields (authority PDA) derived once
  obs::DecisionLog* log_ = nullptr;
  SendHook send_hook_;
  RuntimeStats stats_;
  int64_t wall_ms_ = 0;
  uint64_t frame_recv_ns_ = 0;
  uint64_t inputs_slot_ = 0;
};

}  // namespace arb::app
