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

/** Read `keys` (batches of 100) and apply them to the store at each batch's context slot. Throws on RPC failure. */
ResyncStats resync(Rpc& rpc, StateStore& store, const std::vector<Pubkey>& keys);

}  // namespace arb::feed
