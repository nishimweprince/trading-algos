#include "risk/sign_guard.hpp"

#include <algorithm>
#include <cstring>

#include "core/base58.hpp"
#include "core/programs.hpp"

namespace arb::risk {

const std::vector<Pubkey>& default_tip_accounts() {
  static const std::vector<Pubkey> v = {
      PK("96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5"), PK("HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe"),
      PK("Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY"), PK("ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49"),
      PK("DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh"), PK("ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt"),
      PK("DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL"), PK("3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT"),
  };
  return v;
}

Result<void> SignGuard::check(const tx::IxList& ixs) const {
  int transfers = 0;
  for (uint8_t i = 0; i < ixs.n; ++i) {
    const tx::Ix& ix = ixs.ixs[i];
    const Pubkey& p = ix.program;
    bool allowed = p == prog::COMPUTE_BUDGET || p == prog::SYSTEM || p == prog::KLEND || p == executor || p == prog::TOKEN ||
                   p == prog::TOKEN_2022 || p == prog::ATA;
    if (!allowed || is_zero(executor)) return fail(Errc::sign_guard, "program not in allowlist");
    if (p == prog::SYSTEM) {
      uint32_t tag = 0;
      uint64_t lamports = 0;
      if (ix.data_len != 12 || ix.n_accounts != 2) return fail(Errc::sign_guard, "system instruction is not a transfer");
      std::memcpy(&tag, ix.data, 4);
      std::memcpy(&lamports, ix.data + 4, 8);
      if (tag != 2) return fail(Errc::sign_guard, "system instruction is not a transfer");
      if (ix.accounts[0].key != wallet) return fail(Errc::sign_guard, "transfer not from the wallet");
      if (std::find(tip_accounts.begin(), tip_accounts.end(), ix.accounts[1].key) == tip_accounts.end())
        return fail(Errc::sign_guard, "transfer not to a Jito tip account");
      if (lamports > tip_max_lamports) return fail(Errc::sign_guard, "tip above tip_max");
      ++transfers;
    }
    if (p == executor && (ix.n_accounts == 0 || ix.accounts[0].key != wallet || !ix.accounts[0].signer))
      return fail(Errc::sign_guard, "executor signer is not the wallet");
  }
  if (transfers != 1) return fail(Errc::sign_guard, "exactly one tip transfer required");
  return {};
}

}  // namespace arb::risk
