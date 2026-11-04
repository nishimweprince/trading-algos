#pragma once
// Applies parsed frames to the StateStore and keeps the slot clock. Shared by the
// live LaserStream path, the recorder (which also persists frames) and replay.
#include <atomic>
#include <cstdint>

#include "feed/update.hpp"
#include "state/state_store.hpp"

namespace arb::feed {

/** Newest slot seen on any slots stream, with the monotonic time it arrived. */
struct SlotClock {
  std::atomic<uint64_t> slot{0};
  std::atomic<uint64_t> at_ns{0};
  void observe(uint64_t s, uint64_t now_ns) {
    if (s > slot.load(std::memory_order_relaxed)) {
      slot.store(s, std::memory_order_relaxed);
      at_ns.store(now_ns, std::memory_order_relaxed);
    }
  }
};

class FeedHandler {
 public:
  FeedHandler(StateStore& store, SlotClock& clock) : store_(store), clock_(clock) {}
  /** Parse and apply one raw frame. False when malformed or undecodable. */
  bool on_frame(const uint8_t* data, size_t len, uint64_t recv_ns);
  uint64_t frames() const { return frames_; }
  uint64_t bad_frames() const { return bad_; }
  uint64_t account_updates() const { return accounts_; }
  uint64_t last_update_ns() const { return last_update_ns_.load(std::memory_order_relaxed); }

 private:
  StateStore& store_;
  SlotClock& clock_;
  UpdateView view_;
  uint64_t frames_ = 0, bad_ = 0, accounts_ = 0;
  std::atomic<uint64_t> last_update_ns_{0};
};

}  // namespace arb::feed
