#pragma once
// Applies parsed frames to the StateStore and keeps the slot clock. Shared by the
// live LaserStream path, the recorder (which also persists frames) and replay.
#include <atomic>
#include <cstdint>

#include <unordered_map>
#include <utility>
#include <vector>

#include "feed/update.hpp"
#include "obs/latency.hpp"
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
  /** Account updates: our receive time minus the server's created_at (Helius -> us), microseconds. */
  obs::LatencyWindow& feed_lag_us() { return feed_lag_us_; }
  /** Account updates: receive time minus the moment their slot started (first shred / bank created), microseconds. */
  obs::LatencyWindow& slot_offset_us() { return slot_offset_us_; }

  /**
   * Alpenglow: a slot can carry several candidate blocks (banks); Geyser tags each update with its bank_id
   * (node-local, so only compared within this one connection). While a slot has updates from two or more
   * banks, the accounts they touched are suspect (not quotable). When the slot's bank is confirmed, every
   * account a losing bank touched is queued here for a trusted RPC re-read.
   */
  std::vector<Pubkey> take_resync() { return std::exchange(resync_, {}); }
  struct BankCounters {
    uint64_t updates_with_bank = 0, created_banks = 0, contested_slots = 0, resolved_slots = 0, accounts_resynced = 0;
  };
  const BankCounters& banks() const { return bank_n_; }

 private:
  StateStore& store_;
  SlotClock& clock_;
  UpdateView view_;
  uint64_t frames_ = 0, bad_ = 0, accounts_ = 0;
  std::atomic<uint64_t> last_update_ns_{0};
  obs::LatencyWindow feed_lag_us_, slot_offset_us_;
  struct SlotBanks {
    std::vector<uint64_t> banks;
    std::vector<std::pair<Pubkey, uint64_t>> touched;  // (account, bank) applied in this slot
    bool contested = false;
  };
  void note_bank(uint64_t slot, uint64_t bank);
  void resolve(uint64_t slot, uint64_t winner);
  std::unordered_map<uint64_t, SlotBanks> slot_banks_;
  std::vector<Pubkey> resync_;
  BankCounters bank_n_;
  std::unordered_map<uint64_t, uint64_t> slot_start_ns_;  // slot -> earliest interslot status time (monotonic)
  int64_t wall_minus_mono_ns_ = 0;                         // refreshed every 4096 frames
};

}  // namespace arb::feed
