#include "risk/token_safety.hpp"

#include <algorithm>

#include "core/programs.hpp"

namespace arb::risk {

SafetyVerdict check_mint(const Pubkey& key, const state::Mint& m, const SafetyPolicy& p) {
  using namespace state::ext;
  if (m.token_program != prog::TOKEN && m.token_program != prog::TOKEN_2022) return {false, "unknown token program"};
  if (m.has_ext(TransferHook)) return {false, "transfer_hook"};
  if (m.has_ext(PermanentDelegate)) return {false, "permanent_delegate"};
  if (m.has_ext(NonTransferable)) return {false, "non_transferable"};
  if (m.has_ext(ConfidentialTransferMint) || m.has_ext(ConfidentialTransferFeeConfig)) return {false, "confidential_transfer"};
  if (m.has_ext(Pausable)) return {false, "pausable"};
  if (m.has_ext(DefaultAccountState) && m.default_account_state == 2) return {false, "default_frozen"};
  if (m.has_transfer_fee) {
    if (!p.quoter_handles_transfer_fee) return {false, "transfer_fee_unsupported"};
    int bps = std::max(m.older_fee.basis_points, m.newer_fee.basis_points);
    if (bps > p.transfer_fee_max_bps) return {false, "transfer_fee_too_high"};
  }
  if (m.has_freeze_authority && std::find(p.freeze_allowlist.begin(), p.freeze_allowlist.end(), key) == p.freeze_allowlist.end())
    return {false, "freeze_authority"};
  return {};
}

SafetyVerdict check_pool_ownership(const Pubkey& pool_owner, const Pubkey& dex_program, const Pubkey& va, const Pubkey& vb) {
  if (pool_owner != dex_program) return {false, "pool_owner"};
  auto tok = [](const Pubkey& o) { return o == prog::TOKEN || o == prog::TOKEN_2022; };
  if (!tok(va) || !tok(vb)) return {false, "vault_owner"};
  return {};
}

}  // namespace arb::risk
