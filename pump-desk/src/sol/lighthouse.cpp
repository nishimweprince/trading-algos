#include "sol/lighthouse.hpp"

#include "sol/constants.hpp"

namespace pd::lighthouse {

namespace {
constexpr uint8_t LOG_SILENT = 0;
constexpr uint8_t TOKEN_ACCOUNT_AMOUNT = 2;  // TokenAccountAssertion: Mint, Owner, Amount, ...
constexpr uint8_t ACCOUNT_INFO_LAMPORTS = 0; // AccountInfoAssertion: Lamports, DataLength, Owner, ...
}  // namespace

void assert_token_amount(IxList& l, const Pubkey& token_account, IntOp op, uint64_t value) {
  l.add(prog::LIGHTHOUSE).ro(token_account).u8(ASSERT_TOKEN_ACCOUNT).u8(LOG_SILENT).u8(TOKEN_ACCOUNT_AMOUNT).u64(value).u8(uint8_t(op));
}

void assert_lamports(IxList& l, const Pubkey& account, IntOp op, uint64_t value) {
  l.add(prog::LIGHTHOUSE).ro(account).u8(ASSERT_ACCOUNT_INFO).u8(LOG_SILENT).u8(ACCOUNT_INFO_LAMPORTS).u64(value).u8(uint8_t(op));
}

}  // namespace pd::lighthouse
