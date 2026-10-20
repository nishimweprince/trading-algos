#pragma once
// geyser.SubscribeRequest encoding (off the hot path; allocates freely).
#include <cstdint>
#include <string>
#include <vector>

#include "core/types.hpp"

namespace pd {

struct SubscriptionSpec {
  /** Stream curves with < band_values * 2^40 raw tokens left (src/racer/stream.ts). */
  int band_values = 12;
  Pubkey wallet{};
  /** Pools / vaults of fired curves and open positions. */
  std::vector<Pubkey> watched;
  /** Fresh pools whose every tx is streamed for a few slots after the migrate (rival profiling). */
  std::vector<Pubkey> pool_txs;
};

/** The full request; Yellowstone replaces the whole subscription on every write. */
std::string encode_subscribe_request(const SubscriptionSpec& spec);

/** A keepalive ping request (SubscribeRequest.ping). */
std::string encode_ping_request(int32_t id);

}  // namespace pd
