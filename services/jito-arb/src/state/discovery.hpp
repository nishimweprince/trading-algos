#pragma once
// Pool discovery (spec §4), off the hot path: getProgramAccounts results per DEX ->
// token graph -> TVL filter -> safety gate -> tracked set and subscription diff.
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

#include "core/types.hpp"
#include "risk/token_safety.hpp"
#include "state/accounts.hpp"
#include "state/state_store.hpp"

namespace arb::state {

struct RawAccount {
  Pubkey key{};
  Pubkey owner{};
  std::vector<uint8_t> data;
};

/** Parse a getProgramAccounts JSON-RPC response body (base64 encoding). */
std::vector<RawAccount> parse_gpa_response(std::string_view body);

struct PoolCandidate {
  PoolDef def;
  double tvl_sol = 0;
  std::string reject;  // empty when tracked
};

struct DiscoveryConfig {
  std::vector<Pubkey> base_mints;
  double min_pool_tvl_sol = 50;
  size_t max_tracked_pools = 1500;
  risk::SafetyPolicy safety;
  int tick_arrays_below = 2, tick_arrays_above = 3;  // around the current array (b->a can shift one up)
};

struct DiscoveryResult {
  std::vector<PoolCandidate> tracked;
  std::vector<PoolCandidate> rejected;
  std::map<Pubkey, std::vector<size_t>> graph;  // mint -> indexes into `tracked`
  double sol_price_usdc = 0;                    // from the deepest SOL/USDC pool
  std::map<std::string, size_t> reject_counts;
};

/** Account lookup for vaults and mints (from getMultipleAccounts on the VM, fixtures in tests). */
using AccountLookup = std::unordered_map<Pubkey, RawAccount, PubkeyHash>;

/** Build a candidate PoolDef from a raw pool account; nullopt if it does not decode. */
std::optional<PoolDef> pool_def_from_account(const RawAccount& a, const DiscoveryConfig& cfg, bool include_arrays = true);

/** The vault and mint keys `pools` need (for one getMultipleAccounts round). */
std::vector<Pubkey> dependent_keys(const std::vector<RawAccount>& pools, const DiscoveryConfig& cfg);

DiscoveryResult discover(const std::vector<RawAccount>& pool_accounts, const AccountLookup& accounts, const DiscoveryConfig& cfg);

/** Whirlpool tick-array PDA: ["tick_array", whirlpool, start_tick_index as decimal string]. */
Pubkey tick_array_pda(const Pubkey& whirlpool, int32_t start_tick_index);
int32_t tick_array_start(int32_t tick, uint16_t tick_spacing);

}  // namespace arb::state
