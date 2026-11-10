#pragma once
// The live-setup plan (docs/LIVE-READINESS.md): which pools to arm, and what arming them costs in rent.
// Pure functions over the decision log and pools.json; the live-plan tool prints the result. Nothing is sent.
#include <cstdint>
#include <string>
#include <utility>
#include <vector>

#include "app/pools_file.hpp"
#include "obs/decision_log.hpp"

namespace arb::app {

/** Rent-exempt minimum for an account of `bytes` (2 years at 3,480 lamports per byte-year, 128 bytes of overhead). */
constexpr uint64_t rent_exempt_lamports(uint64_t bytes) { return (bytes + 128) * 6960; }
constexpr uint64_t TOKEN_ACCOUNT_BYTES = 165;   // classic SPL; Token-2022 accounts with extensions are larger
constexpr uint64_t ALT_META_BYTES = 56;
constexpr size_t ALT_ADDRESSES = 256;

struct PoolScore {
  Pubkey pool{};
  uint32_t passes = 0;     // guard-passing fresh simulations whose route used the pool
  int64_t net_lamports = 0;
};

/**
 * Pools ranked by how often they sat on a dry-run route that would have landed: simulated OK, net at or above
 * min_net (the on-chain guard's margin), and simulated within max_lag_slots of the candidate. Records may be
 * in any order; candidates and outcomes are joined by signature.
 */
std::vector<PoolScore> rank_pools(const std::vector<obs::DecisionRecord>& records, int64_t min_net_lamports, uint64_t max_lag_slots);

struct LivePlan {
  std::vector<Pubkey> armed;       // the top max_pools of the ranking that are still in pools.json
  size_t dropped = 0;              // ranked pools no longer tracked
  size_t table_keys = 0, tables = 0;
  uint64_t table_rent_lamports = 0;
  size_t token_accounts = 0;       // one per non-base mint of the armed pools
  uint64_t token_rent_lamports = 0;
  uint64_t total_rent_lamports() const { return table_rent_lamports + token_rent_lamports; }
};

/** `extra_keys`: keys every route also references (programs, authorities, the wallet's accounts, flash reserve). */
LivePlan size_plan(const PoolsFile& pools, const std::vector<PoolScore>& ranking, size_t max_pools,
                   const std::vector<Pubkey>& base_mints, size_t extra_keys = 40);

std::string plan_report(const LivePlan& p, size_t ranked, size_t max_pools);

}  // namespace arb::app
