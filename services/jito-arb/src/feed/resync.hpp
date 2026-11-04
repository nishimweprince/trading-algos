#pragma once
// Resync (spec §4): on reconnect, or when a pool lags the slot clock by more than
// resync_lag_slots, re-read its accounts with getMultipleAccounts in batches of 100.
#include <vector>

#include "core/rpc.hpp"
#include "state/state_store.hpp"

namespace arb::feed {

struct ResyncStats {
  size_t requested = 0, applied = 0, missing = 0, failed = 0, batches = 0;
};

/**
 * Read `keys` (batches of 100) and apply them to the store at each batch's context slot. Throws on RPC failure.
 * `stream_live`: every stream was already subscribed before the read, so no later change can be missed and the
 * values are usable at once. Otherwise they wait for a fresh push (spec §4: a gap may hide a change).
 */
ResyncStats resync(Rpc& rpc, StateStore& store, const std::vector<Pubkey>& keys, bool stream_live = false);

/** The store's keys grouped pool by pool (pool, vaults, mints, tick arrays), so one pool's accounts share a batch. */
std::vector<Pubkey> pool_ordered_keys(const StateStore& store, const std::vector<uint32_t>& pool_ids);

}  // namespace arb::feed
