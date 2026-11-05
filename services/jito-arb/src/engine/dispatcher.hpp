#pragma once
// Dispatch and conflicts (spec §6.7): candidates of one engine pass go into a max-heap by EV;
// the best are taken while they share no pool with a bundle already sent this slot, at most
// max_bundles_per_slot per slot, dropping any whose state is more than max_send_lag_slots old;
// a sent cycle cools down for cooldown_slots. (Both come from millisecond settings via chain.slot_ms.)
#include <cstdint>
#include <functional>
#include <queue>
#include <unordered_map>
#include <vector>

#include "engine/costs.hpp"
#include "engine/route_graph.hpp"

namespace arb::engine {

struct Opportunity {
  uint32_t cycle = 0;
  uint64_t slot = 0;        // slot of the update that triggered the evaluation
  uint64_t state_slot = 0;  // oldest leg snapshot's newest component slot
  uint64_t x = 0;
  uint64_t final_out = 0;
  uint64_t flash_fee = 0;
  int64_t profit_base = 0;
  uint64_t leg_out[MAX_LEGS]{};
  uint64_t leg_slot[MAX_LEGS]{}, leg_seq[MAX_LEGS]{};
  Pricing pr;
};

struct DispatchConfig {
  int max_bundles_per_slot = 3;
  int cooldown_slots = 2;
  int max_send_lag_slots = 1;
};

class Dispatcher {
 public:
  explicit Dispatcher(DispatchConfig cfg = {}) : cfg_(cfg) {}
  void push(const Opportunity& o) { heap_.push(o); }
  size_t pending() const { return heap_.size(); }
  bool cooling(uint32_t cycle, uint64_t slot) const;
  /**
   * Pop every pending candidate: `send` for the chosen ones, `skip` (with the reason) for the rest.
   * `current_slot` is the slot clock at dispatch time.
   */
  void dispatch(uint64_t current_slot, const CycleIndex& idx, const std::function<void(const Opportunity&)>& send,
                const std::function<void(const Opportunity&, Reason)>& skip);

 private:
  struct ByEv {
    bool operator()(const Opportunity& a, const Opportunity& b) const { return a.pr.ev_lamports < b.pr.ev_lamports; }
  };
  DispatchConfig cfg_;
  std::priority_queue<Opportunity, std::vector<Opportunity>, ByEv> heap_;
  std::unordered_map<uint32_t, uint64_t> cooldown_until_;  // cycle -> last slot it is still cooling
  uint64_t sent_slot_ = UINT64_MAX;
  int sent_in_slot_ = 0;
  std::vector<uint32_t> pools_in_slot_;
};

}  // namespace arb::engine
