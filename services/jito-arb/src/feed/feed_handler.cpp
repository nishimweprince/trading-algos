#include "feed/feed_handler.hpp"

namespace arb::feed {

bool FeedHandler::on_frame(const uint8_t* data, size_t len, uint64_t recv_ns) {
  ++frames_;
  if (!parse_update(data, len, view_)) {
    ++bad_;
    return false;
  }
  if (view_.kind == UpdateView::Slot) {
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
  if (!store_.apply(u)) {
    ++bad_;
    return false;
  }
  return true;
}

}  // namespace arb::feed
