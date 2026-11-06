#pragma once
// Direct DEX swap instructions. Account order and data layouts are the programs':
//   Raydium AMM v4 SwapBaseInV2 (tag 16): program/src/instruction.rs swap_base_in_v2 (raydium-amm@91739d8)
//   Whirlpool swap: programs/whirlpool/src/instructions/swap.rs (whirlpools@cc683ec)
//   Raydium CPMM swap_base_input: programs/cp-swap/src/instructions/swap_base_input.rs (raydium-cp-swap@3d69122)
// The sim-parity oracle sends exactly these instructions through simulateTransaction, which is how the
// account order is proven; the executor's CPI legs reuse the same account lists (spec §8).
#include <array>
#include <vector>

#include "state/pool_state.hpp"
#include "tx/message_v0.hpp"

namespace arb::tx::dexix {

inline constexpr uint8_t WHIRLPOOL_SWAP_DISC[8] = {248, 198, 158, 145, 225, 117, 135, 200};  // sha256("global:swap")[..8]
constexpr uint8_t RAYDIUM_SWAP_BASE_IN_V2 = 16;
inline constexpr uint8_t PUMP_SELL_V2_DISC[8] = {93, 246, 130, 60, 231, 233, 64, 178};
inline constexpr uint8_t PUMP_BUY_EXACT_QUOTE_IN_V2_DISC[8] = {194, 171, 28, 70, 104, 77, 91, 47};
inline constexpr uint8_t CPMM_SWAP_BASE_INPUT_DISC[8] = {143, 190, 90, 218, 196, 30, 51, 222};  // global:swap_base_input

const Pubkey& raydium_authority();  // PDA ["amm authority"] of the v4 program
Pubkey whirlpool_oracle(const Pubkey& whirlpool);
const Pubkey& cpmm_authority();  // PDA ["vault_and_lp_mint_auth_seed"] of the CPMM program
const Pubkey& pump_global_config();  // PumpSwap PDA ["global_config"]
const Pubkey& pump_fee_config();     // Pump Fees PDA ["fee_config", PumpSwap program]
const Pubkey& pump_event_authority();
Pubkey pump_user_volume_accumulator(const Pubkey& user);
void pump_init_user_volume(tx::IxList& ixs, const Pubkey& user);
void pump_fund_user_volume(tx::IxList& ixs, const Pubkey& user, uint64_t lamports);

/** The three tick-array PDAs a swap in this direction walks (sparse_swap.rs get_start_tick_indexes). */
std::array<Pubkey, 3> whirlpool_tick_arrays(const state::Whirlpool& w, const Pubkey& pool, bool a_to_b);

struct UserAccounts {
  Pubkey authority{};      // signer that owns the input token account
  Pubkey token_a{};        // user token account for mint a (Raydium coin / Whirlpool token A)
  Pubkey token_b{};        // user token account for mint b
};

/** Account list of one swap leg, in the program's order (no program id). */
std::vector<AccountMeta> swap_accounts(const PoolState& p, bool a_to_b, const UserAccounts& u);

/** Instruction data of one swap leg (exact in, min out). */
std::vector<uint8_t> swap_data(DexKind kind, bool a_to_b, uint64_t amount_in, uint64_t min_out);

/** Append the full swap instruction to `l`. */
void swap(IxList& l, const PoolState& p, bool a_to_b, const UserAccounts& u, uint64_t amount_in, uint64_t min_out);

const Pubkey& program_of(DexKind kind);

}  // namespace arb::tx::dexix
