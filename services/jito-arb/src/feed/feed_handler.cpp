#include "feed/feed_handler.hpp"

#include <algorithm>
#include <ctime>

namespace arb::feed {

namespace {
int64_t wall_minus_mono() {
  timespec w, m;
  clock_gettime(CLOCK_REALTIME, &w);
  clock_gettime(CLOCK_MONOTONIC, &m);
  return (int64_t(w.tv_sec) - int64_t(m.tv_sec)) * 1'000'000'000LL + (int64_t(w.tv_nsec) - int64_t(m.tv_nsec));
}
constexpr uint32_t SLOT_CONFIRMED = 1, SLOT_FINALIZED = 2, SLOT_FIRST_SHRED_RECEIVED = 3, SLOT_CREATED_BANK = 5;
}  // namespace

void FeedHandler::note_bank(uint64_t slot, uint64_t bank) {
  SlotBanks& sb = slot_banks_[slot];
  if (std::find(sb.banks.begin(), sb.banks.end(), bank) != sb.banks.end()) return;
  sb.banks.push_back(bank);
  if (sb.banks.size() >= 2 && !sb.contested) {
    sb.contested = true;
    ++bank_n_.contested_slots;
    for (auto& [k, b] : sb.touched) store_.mark_suspect(k);
  }
}

void FeedHandler::resolve(uint64_t slot, uint64_t winner) {
  auto it = slot_banks_.find(slot);
  if (it == slot_banks_.end()) return;
  bool any_loser = false;
  for (auto& [k, b] : it->second.touched)
    if (b != winner) {
      store_.mark_suspect(k);
      resync_.push_back(k);
      ++bank_n_.accounts_resynced;
      any_loser = true;
    }
  if (it->second.contested || any_loser) ++bank_n_.resolved_slots;
  slot_banks_.erase(it);
}

bool FeedHandler::on_frame(const uint8_t* data, size_t len, uint64_t recv_ns) {
  if ((frames_++ & 4095) == 0) wall_minus_mono_ns_ = wall_minus_mono();
  if (!parse_update(data, len, view_)) {
    ++bad_;
    return false;
  }
  if (view_.kind == UpdateView::Slot) {
    uint32_t st = view_.slot.status;
    if (view_.slot.has_bank) {
      if (st == SLOT_CREATED_BANK) {
        ++bank_n_.created_banks;
        note_bank(view_.slot.slot, view_.slot.bank_id);
      } else if (st == SLOT_CONFIRMED || st == SLOT_FINALIZED) {
        resolve(view_.slot.slot, view_.slot.bank_id);
      }
    }
    // Forget slots far behind the clock (a resolution that never arrived).
    if (slot_banks_.size() > 1024)
      for (auto it = slot_banks_.begin(); it != slot_banks_.end();)
        it = it->first + 512 < clock_.slot.load(std::memory_order_relaxed) ? slot_banks_.erase(it) : std::next(it);
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
  if (a.has_bank) {
    ++bank_n_.updates_with_bank;
    note_bank(a.slot, a.bank_id);
    SlotBanks& sb = slot_banks_[a.slot];
    sb.touched.emplace_back(u.key, a.bank_id);
    if (sb.contested) store_.mark_suspect(u.key);
  }
  return true;
}

}  // namespace arb::feed
