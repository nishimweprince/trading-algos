#include "feed/subscription.hpp"

#include <algorithm>

#include "core/base58.hpp"
#include "core/pb.hpp"

namespace arb::feed {

std::string encode_subscribe_request(const ShardSpec& spec) {
  pb::Writer req;
  // accounts (1): SubscribeRequestFilterAccounts { account = 2 (repeated base58) }
  if (!spec.accounts.empty()) {
    pb::Writer a;
    for (const Pubkey& k : spec.accounts) a.bytes(2, b58(k));
    req.map_entry(1, "accounts", a);
  }
  // slots (2): SubscribeRequestFilterSlots { filter_by_commitment = 1, interslot_updates = 2 }. Interslot
  // updates (first shred received, bank created) mark when each slot starts, for the slot-offset metric.
  if (spec.slots) {
    pb::Writer s;
    s.boolean(1, false);
    s.boolean(2, true);
    req.map_entry(2, "slots", s);
  }
  // commitment (6) = PROCESSED (0): optional, so presence is explicit.
  req.u64(6, 0);
  // from_slot (11): replay from the last seen slot after a reconnect.
  if (spec.from_slot) req.u64(11, *spec.from_slot);
  return req.out;
}

std::string encode_ping_request(int32_t id) {
  pb::Writer ping;
  ping.u64(1, static_cast<uint64_t>(static_cast<uint32_t>(id)));
  pb::Writer req;
  req.message(9, ping);
  return req.out;
}

SubscriptionPlan plan_subscriptions(std::vector<Pubkey> keys, const std::vector<Pubkey>& wallet_keys, int max_pool_conns,
                                    int shard_size) {
  SubscriptionPlan p;
  std::sort(keys.begin(), keys.end());
  keys.erase(std::unique(keys.begin(), keys.end()), keys.end());
  size_t cap = size_t(std::max(1, max_pool_conns)) * size_t(std::max(1, shard_size));
  if (keys.size() > cap) {
    p.overflow = keys.size() - cap;
    keys.resize(cap);
  }
  size_t n_shards = std::max<size_t>(1, (keys.size() + size_t(shard_size) - 1) / size_t(shard_size));
  p.pool_shards.resize(n_shards);
  for (size_t i = 0; i < keys.size(); ++i) p.pool_shards[i % n_shards].accounts.push_back(keys[i]);
  // Only one connection carries slots for the clock; the others stay account-only.
  for (size_t i = 0; i < p.pool_shards.size(); ++i) p.pool_shards[i].slots = (i == 0);
  p.wallet_shard.accounts = wallet_keys;
  p.wallet_shard.slots = true;
  return p;
}

SubscriptionDiff diff_keys(std::vector<Pubkey> before, std::vector<Pubkey> after) {
  std::sort(before.begin(), before.end());
  std::sort(after.begin(), after.end());
  SubscriptionDiff d;
  std::set_difference(after.begin(), after.end(), before.begin(), before.end(), std::back_inserter(d.added));
  std::set_difference(before.begin(), before.end(), after.begin(), after.end(), std::back_inserter(d.removed));
  return d;
}

}  // namespace arb::feed
