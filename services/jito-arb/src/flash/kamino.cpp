#include "flash/kamino.hpp"

#include "core/crypto.hpp"
#include "core/programs.hpp"

namespace arb::flash {

Result<uint64_t> flash_loan_fee(uint64_t amount, uint64_t fee_sf) {
  if (fee_sf == UINT64_MAX) return fail(Errc::disabled, "FlashLoansDisabled");
  if (fee_sf == 0 || amount == 0) return uint64_t(0);
  constexpr u128 ONE = u128(1) << 60;
  u128 fee_f = u128(amount) * fee_sf;          // (amount << 60) * fee_sf >> 60, exact
  if (fee_f < ONE) fee_f = ONE;                // .max(minimum_fee = 1)
  if (fee_f >= u128(amount) << 60) return fail(Errc::insufficient_liquidity, "BorrowTooSmall");
  return uint64_t((fee_f + (ONE >> 1)) >> 60);  // round(): half away from zero
}

FlashReserve make_reserve(const Pubkey& key, const state::KaminoReserve& r, uint64_t slot) {
  FlashReserve f;
  f.reserve = key;
  f.lending_market = r.lending_market;
  f.lending_market_authority = pda({std::string_view("lma"), r.lending_market}, prog::KLEND);
  f.liquidity_mint = r.liquidity_mint;
  f.supply_vault = r.supply_vault;
  f.fee_vault = r.fee_vault;
  f.token_program = r.token_program;
  f.flash_loan_fee_sf = r.flash_loan_fee_sf;
  f.available = r.available_amount;
  f.slot = slot;
  f.enabled = r.flash_enabled();
  return f;
}

namespace {
// Shared account list: authority (signer), lending_market_authority, lending_market, reserve (w),
// reserve_liquidity_mint, reserve liquidity (w), user liquidity (w), fee receiver (w),
// referrer_token_state = program id (None), referrer_account = program id (None), sysvar instructions,
// token program. Borrow lists (source = supply vault, destination = user); repay (destination = supply
// vault, source = user): the same positions.
void accounts(tx::Ix& ix, const FlashReserve& r, const Pubkey& user, const Pubkey& user_ata) {
  ix.acc(user, true, false)
      .ro(r.lending_market_authority)
      .ro(r.lending_market)
      .rw(r.reserve)
      .ro(r.liquidity_mint)
      .rw(r.supply_vault)
      .rw(user_ata)
      .rw(r.fee_vault)
      .ro(prog::KLEND)
      .ro(prog::KLEND)
      .ro(prog::SYSVAR_INSTRUCTIONS)
      .ro(r.token_program);
}
}  // namespace

void borrow_ix(tx::IxList& l, const FlashReserve& r, const Pubkey& user, const Pubkey& user_ata, uint64_t amount) {
  tx::Ix& ix = l.add(prog::KLEND);
  accounts(ix, r, user, user_ata);
  ix.bytes(DISC_FLASH_BORROW, 8).u64(amount);
}

void repay_ix(tx::IxList& l, const FlashReserve& r, const Pubkey& user, const Pubkey& user_ata, uint64_t amount,
              uint8_t borrow_instruction_index) {
  tx::Ix& ix = l.add(prog::KLEND);
  accounts(ix, r, user, user_ata);
  ix.bytes(DISC_FLASH_REPAY, 8).u64(amount).u8(borrow_instruction_index);
}

}  // namespace arb::flash
