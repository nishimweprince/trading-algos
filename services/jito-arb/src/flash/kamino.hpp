#pragma once
// Kamino Lend flash loans (spec §7). Ported from Kamino-Finance/klend@77e8185:
//   programs/klend/src/state/reserve.rs ReserveFees::calculate_flash_loan_fees / calculate_fees
//   programs/klend/src/lending_market/lending_operations.rs flash_borrow/flash_repay_reserve_liquidity
//   libs/klend-interface/src/instructions/flash.rs (account order and data)
#include <cstdint>

#include "core/error.hpp"
#include "core/types.hpp"
#include "state/accounts.hpp"
#include "tx/message_v0.hpp"

namespace arb::flash {

inline constexpr uint8_t DISC_FLASH_BORROW[8] = {135, 231, 52, 167, 7, 52, 212, 193};  // global:flash_borrow_reserve_liquidity
inline constexpr uint8_t DISC_FLASH_REPAY[8] = {185, 117, 0, 203, 96, 245, 180, 186};   // global:flash_repay_reserve_liquidity

/**
 * The protocol fee the repay instruction pulls on top of `amount`, with no referrer:
 *   fee_f = max(amount * flash_loan_fee_sf / 2^60, 1)   (U68F60; the product is exact)
 *   fee   = round_half_up(fee_f)                        (Fraction::to_round)
 * flash_loan_fee_sf == 0 -> 0; u64::MAX -> disabled (FlashLoansDisabled); fee_f >= amount -> BorrowTooSmall.
 */
Result<uint64_t> flash_loan_fee(uint64_t amount, uint64_t flash_loan_fee_sf);

/** Per base token: everything the borrow/repay pair needs (spec §7.4). */
struct FlashReserve {
  Pubkey reserve{}, lending_market{}, lending_market_authority{};
  Pubkey liquidity_mint{}, supply_vault{}, fee_vault{}, token_program{};
  uint64_t flash_loan_fee_sf = UINT64_MAX;
  uint64_t available = 0;
  uint64_t slot = 0;
  bool enabled = false;
};

/** lending_market_authority = PDA ["lma", lending_market] under klend. */
FlashReserve make_reserve(const Pubkey& reserve_key, const state::KaminoReserve& r, uint64_t slot);

/** flash_borrow_reserve_liquidity(amount): destination = the bot's base-token ATA. No referrer. */
void borrow_ix(tx::IxList& l, const FlashReserve& r, const Pubkey& user, const Pubkey& user_ata, uint64_t amount);
/** flash_repay_reserve_liquidity(amount, borrow_instruction_index): source = the same ATA. */
void repay_ix(tx::IxList& l, const FlashReserve& r, const Pubkey& user, const Pubkey& user_ata, uint64_t amount,
              uint8_t borrow_instruction_index);

}  // namespace arb::flash
