#pragma once
// Pre-sign allowlist check (spec §10, key security): the hot wallet signs only transactions whose every
// top-level instruction is from an allowed program (ComputeBudget, System, Kamino, the executor, SPL
// Token, Token-2022, Associated Token), and whose only System instruction is one transfer from the
// wallet to a cached Jito tip account of at most tip_max lamports.
#include <cstdint>
#include <vector>

#include "core/error.hpp"
#include "core/types.hpp"
#include "tx/message_v0.hpp"

namespace arb::risk {

/** Jito tip accounts as documented (getTipAccounts example; "have remained constant"). Refreshed live hourly. */
const std::vector<Pubkey>& default_tip_accounts();

struct SignGuard {
  Pubkey wallet{};
  Pubkey executor{};
  std::vector<Pubkey> tip_accounts;
  uint64_t tip_max_lamports = 50'000'000;

  /** ok, or Errc::sign_guard with the rule that failed in `what`. */
  Result<void> check(const tx::IxList& ixs) const;
};

}  // namespace arb::risk
