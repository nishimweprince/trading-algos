#include "engine/dispatcher.hpp"

#include <algorithm>

namespace arb::engine {

bool Dispatcher::cooling(uint32_t cycle, uint64_t slot) const {
  auto it = cooldown_until_.find(cycle);
  return it != cooldown_until_.end() && slot <= it->second;
}

void Dispatcher::dispatch(uint64_t current_slot, const CycleIndex& idx, const std::function<void(const Opportunity&)>& send,
                          const std::function<void(const Opportunity&, Reason)>& skip) {
  if (current_slot != sent_slot_) {
    sent_slot_ = current_slot;
    sent_in_slot_ = 0;
    pools_in_slot_.clear();
  }
  while (!heap_.empty()) {
    Opportunity o = heap_.top();
    heap_.pop();
    const Cycle& c = idx.cycle(o.cycle);
    // The evaluation's slot, not the legs' last-write slots: a quiet pool's snapshot is still current.
    if (current_slot > o.slot && current_slot - o.slot > uint64_t(cfg_.max_send_lag_slots)) {
      skip(o, Reason::send_lag);
      continue;
    }
    bool conflict = false;
    for (int i = 0; i < c.n; ++i)
      conflict |= std::find(pools_in_slot_.begin(), pools_in_slot_.end(), c.legs[i].pool) != pools_in_slot_.end();
    if (conflict) {
      skip(o, Reason::pool_conflict);
      continue;
    }
    if (sent_in_slot_ >= cfg_.max_bundles_per_slot) {
      skip(o, Reason::slot_cap);
      continue;
    }
    ++sent_in_slot_;
    for (int i = 0; i < c.n; ++i) pools_in_slot_.push_back(c.legs[i].pool);
    cooldown_until_[o.cycle] = current_slot + uint64_t(cfg_.cooldown_slots);
    send(o);
  }
}

}  // namespace arb::engine
