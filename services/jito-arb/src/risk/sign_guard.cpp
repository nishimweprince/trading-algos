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

const std::vector<Pubkey>& sender_tip_accounts() {
  static const std::vector<Pubkey> v = {
      PK("4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE"), PK("D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ"),
      PK("9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta"), PK("5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn"),
      PK("2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD"), PK("2q5pghRs6arqVjRvT5gfgWfWcHWmw1ZuCzphgd5KfWGJ"),
      PK("wyvPkWjVZz1M8fHQnMMCDTQDbkManefNNhweYk5WkcF"),  PK("3KCKozbAaF75qEU33jtzozcJ29yJuaLJTy2jFdzUY8bT"),
      PK("4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey"), PK("4TQLFNWK8AovT1gFvda5jfw2oJeRMKEmw7aH6MGBJ3or"),
  };
  return v;
}

const std::vector<Pubkey>& astralane_tip_accounts() {
  static const std::vector<Pubkey> v = {
      PK("astrazznxsGUhWShqgNtAdfrzP2G83DzcWVJDxwV9bF"), PK("astra4uejePWneqNaJKuFFA8oonqCE1sqF6b45kDMZm"),
      PK("astra9xWY93QyfG6yM8zwsKsRodscjQ2uU2HKNL5prk"), PK("astraRVUuTHjpwEVvNBeQEgwYx9w9CFyfxjYoobCZhL"),
      PK("astraEJ2fEj8Xmy6KLG7B3VfbKfsHXhHrNdCQx7iGJK"), PK("astraubkDw81n4LuutzSQ8uzHCv4BhPVhfvTcYv8SKC"),
      PK("astraZW5GLFefxNPAatceHhYjfA1ciq9gvfEg2S47xk"), PK("astrawVNP4xDBKT7rAdxrLYiTSTdqtUr63fSMduivXK"),
      PK("AstrA1ejL4UeXC2SBP4cpeEmtcFPZVLxx3XGKXyCW6to"), PK("AsTra79FET4aCKWspPqeSFvjJNyp96SvAnrmyAxqg5b7"),
      PK("AstrABAu8CBTyuPXpV4eSCJ5fePEPnxN8NqBaPKQ9fHR"), PK("AsTRADtvb6tTmrsqULQ9Wji9PigDMjhfEMza6zkynEvV"),
      PK("AsTRAEoyMofR3vUPpf9k68Gsfb6ymTZttEtsAbv8Bk4d"), PK("AStrAJv2RN2hKCHxwUMtqmSxgdcNZbihCwc1mCSnG83W"),
      PK("Astran35aiQUF57XZsmkWMtNCtXGLzs8upfiqXxth2bz"), PK("AStRAnpi6kFrKypragExgeRoJ1QnKH7pbSjLAKQVWUum"),
      PK("ASTRaoF93eYt73TYvwtsv6fMWHWbGmMUZfVZPo3CRU9C"),
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
        return fail(Errc::sign_guard, "transfer not to an allowed tip account");
      if (lamports > tip_max_lamports) return fail(Errc::sign_guard, "tip above tip_max");
      ++transfers;
    }
    if (p == executor && (ix.n_accounts == 0 || ix.accounts[0].key != wallet || !ix.accounts[0].signer))
      return fail(Errc::sign_guard, "executor signer is not the wallet");
  }
  if (transfers > 1 || (transfers == 0 && !allow_no_tip)) return fail(Errc::sign_guard, "exactly one tip transfer required");
  return {};
}

}  // namespace arb::risk
