#include "feed/resync.hpp"

#include <algorithm>

namespace arb::feed {

ResyncStats resync(Rpc& rpc, StateStore& store, const std::vector<Pubkey>& keys) {
  ResyncStats st;
  st.requested = keys.size();
  for (size_t i = 0; i < keys.size(); i += 100) {
    std::vector<Pubkey> batch(keys.begin() + long(i), keys.begin() + long(std::min(keys.size(), i + 100)));
    uint64_t slot = 0;
    auto accs = rpc.multiple_accounts_at(batch, slot, "processed");
    ++st.batches;
    for (size_t j = 0; j < batch.size() && j < accs.size(); ++j) {
      AccountUpdate u;
      u.key = batch[j];
      u.slot = slot;
      u.from_resync = true;
      if (!accs[j]) {
        u.missing = true;
        ++st.missing;
      } else {
        u.owner = accs[j]->owner;
        u.data = accs[j]->data.data();
        u.len = accs[j]->data.size();
      }
      if (store.apply(u)) ++st.applied;
      else ++st.failed;
    }
  }
  return st;
}

}  // namespace arb::feed
