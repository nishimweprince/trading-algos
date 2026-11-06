#include "feed/resync.hpp"

#include <algorithm>
#include <set>

namespace arb::feed {

ResyncStats resync(Rpc& rpc, StateStore& store, const std::vector<Pubkey>& keys, bool stream_live) {
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
      u.from_resync = !stream_live;
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

std::vector<Pubkey> pool_ordered_keys(const StateStore& store, const std::vector<uint32_t>& pool_ids) {
  std::vector<Pubkey> out;
  std::set<Pubkey> seen;
  auto add = [&](const Pubkey& k) {
    if (seen.insert(k).second) out.push_back(k);
  };
  for (uint32_t id : pool_ids) {
    const PoolDef& d = store.def(id);
    add(d.address);
    add(d.vault_a);
    add(d.vault_b);
    add(d.mint_a);
    add(d.mint_b);
    for (auto& [start, k] : d.tick_arrays) add(k);
    if (!is_zero(d.config)) add(d.config);
    if (!is_zero(d.fee_config)) add(d.fee_config);
  }
  return out;
}

}  // namespace arb::feed
