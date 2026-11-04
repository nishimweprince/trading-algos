#include "feed/feed_handler.hpp"

#include <ctime>

namespace arb::feed {

namespace {
int64_t wall_minus_mono() {
  timespec w, m;
  clock_gettime(CLOCK_REALTIME, &w);
  clock_gettime(CLOCK_MONOTONIC, &m);
  return (int64_t(w.tv_sec) - int64_t(m.tv_sec)) * 1'000'000'000LL + (int64_t(w.tv_nsec) - int64_t(m.tv_nsec));
}
constexpr uint32_t SLOT_FIRST_SHRED_RECEIVED = 3, SLOT_CREATED_BANK = 5;
}  // namespace

bool FeedHandler::on_frame(const uint8_t* data, size_t len, uint64_t recv_ns) {
  if ((frames_++ & 4095) == 0) wall_minus_mono_ns_ = wall_minus_mono();
  if (!parse_update(data, len, view_)) {
    ++bad_;
    return false;
  }
  if (view_.kind == UpdateView::Slot) {
    uint32_t st = view_.slot.status;
    if (st == SLOT_FIRST_SHRED_RECEIVED || st == SLOT_CREATED_BANK) {
      // A slot start: recorded for the slot-offset metric, but it does not move the slot clock (the bank has
      // not processed anything yet, and the staleness rules count processed slots).
      slot_start_ns_.try_emplace(view_.slot.slot, recv_ns);
      if (slot_start_ns_.size() > 512)
        for (auto it = slot_start_ns_.begin(); it != slot_start_ns_.end();)
          it = it->first + 256 < view_.slot.slot ? slot_start_ns_.erase(it) : std::next(it);
      return true;
    }
    clock_.observe(view_.slot.slot, recv_ns);
    return true;
  }
  if (view_.kind != UpdateView::Account) return true;
  ++accounts_;
  last_update_ns_.store(recv_ns, std::memory_order_relaxed);
  const AccountView& a = view_.account;
  AccountUpdate u;
  u.key = key_from(reinterpret_cast<const uint8_t*>(a.pubkey.data()));
  u.owner = key_from(reinterpret_cast<const uint8_t*>(a.owner.data()));
  u.data = reinterpret_cast<const uint8_t*>(a.data.data());
  u.len = a.data.size();
  u.slot = a.slot;
  u.write_version = a.write_version;
  clock_.observe(a.slot, recv_ns);
  if (view_.created_at_ns > 0)
    feed_lag_us_.add(recv_ns, (int64_t(recv_ns) + wall_minus_mono_ns_ - view_.created_at_ns) / 1000);
  if (auto it = slot_start_ns_.find(a.slot); it != slot_start_ns_.end() && recv_ns >= it->second)
    slot_offset_us_.add(recv_ns, int64_t(recv_ns - it->second) / 1000);
  if (!store_.apply(u)) {
    ++bad_;
    return false;
  }
  return true;
}

}  // namespace arb::feed
