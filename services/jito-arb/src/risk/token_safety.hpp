#pragma once
// Token safety filter (spec §10), run on every mint before a pool holding it is tracked.
#include <string>
#include <vector>

#include "core/types.hpp"
#include "state/accounts.hpp"

namespace arb::risk {

struct SafetyPolicy {
  std::vector<Pubkey> freeze_allowlist;  // USDC/USDT carry a freeze authority and are allowed explicitly
  int transfer_fee_max_bps = 100;        // 1%
  bool quoter_handles_transfer_fee = true;
};

struct SafetyVerdict {
  bool ok = true;
  const char* reason = "";
};

/**
 * Rejects Token-2022 mints with a transfer hook, permanent delegate, non-transferable flag,
 * default-frozen account state, confidential transfers or pausable config; transfer fees above
 * the cap (either epoch's config); and any freeze authority not on the allowlist.
 */
SafetyVerdict check_mint(const Pubkey& mint_key, const state::Mint& m, const SafetyPolicy& p);

/** Pool-level ownership checks: the pool account is owned by the DEX program and both vaults by a token program. */
SafetyVerdict check_pool_ownership(const Pubkey& pool_owner, const Pubkey& dex_program, const Pubkey& vault_a_owner,
                                   const Pubkey& vault_b_owner);

}  // namespace arb::risk
