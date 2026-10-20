#pragma once
// Lighthouse assertion instructions (lighthouse-sdk-legacy 2.0.1, codama-generated
// serializers). A failed assertion aborts the whole transaction with custom error
// 6001 (AssertionFailed), so a buy that lands behind someone reverts instead of
// filling at up to entry.slippage_pct worse.
#include <cstdint>

#include "core/types.hpp"
#include "sol/tx.hpp"

namespace pd::lighthouse {

/** Instruction discriminators (u8). */
inline constexpr uint8_t ASSERT_ACCOUNT_INFO = 5;
inline constexpr uint8_t ASSERT_TOKEN_ACCOUNT = 9;
/** LighthouseError::AssertionFailed. */
inline constexpr uint32_t ERR_ASSERTION_FAILED = 6001;

enum class IntOp : uint8_t { Equal = 0, NotEqual = 1, GreaterThan = 2, LessThan = 3, GreaterOrEqual = 4, LessOrEqual = 5 };

/** AssertTokenAccount { Amount { value, operator } } on an SPL / Token-2022 account. */
void assert_token_amount(IxList& l, const Pubkey& token_account, IntOp op, uint64_t value);
/** AssertAccountInfo { Lamports { value, operator } }; a missing account has 0 lamports. */
void assert_lamports(IxList& l, const Pubkey& account, IntOp op, uint64_t value);

}  // namespace pd::lighthouse
