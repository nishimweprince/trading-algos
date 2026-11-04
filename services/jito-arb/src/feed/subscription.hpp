#pragma once
// LaserStream subscription plan (spec §4): pool/vault/tick-array accounts in shards
// of ~300 pubkeys over 3–4 connections, plus one connection for the wallet and the
// flash-loan reserves, each with a slots stream. Requests are full replacements
// (Yellowstone replaces the subscription on every write).
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "core/types.hpp"

namespace arb::feed {

struct ShardSpec {
  std::vector<Pubkey> accounts;
  bool slots = true;
  std::optional<uint64_t> from_slot;  // LaserStream replay (only when helius.replay_from_slot is verified on)
};

/** SubscribeRequest bytes: accounts filter "accounts" by pubkey, slots "slots", commitment processed. */
std::string encode_subscribe_request(const ShardSpec& spec);
std::string encode_ping_request(int32_t id);

struct SubscriptionPlan {
  std::vector<ShardSpec> pool_shards;  // one per pool connection
  ShardSpec wallet_shard;              // wallet + flash reserves
  size_t overflow = 0;                 // keys that did not fit max connections x shard size
};

/**
 * Spread `pool_keys` over at most `max_pool_conns` connections of up to `shard_size` keys (round-robin
 * after sorting, so a re-plan with a similar set moves few keys).
 */
SubscriptionPlan plan_subscriptions(std::vector<Pubkey> pool_keys, const std::vector<Pubkey>& wallet_keys,
                                    int max_pool_conns, int shard_size);

struct SubscriptionDiff {
  std::vector<Pubkey> added, removed;
};
SubscriptionDiff diff_keys(std::vector<Pubkey> before, std::vector<Pubkey> after);

}  // namespace arb::feed
