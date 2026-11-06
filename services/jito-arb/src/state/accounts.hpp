#pragma once
// Decoded account layouts. Each decoder checks the owner program, the exact
// length (or minimum, for TLV/variable accounts) and the discriminator before
// reading a field. Sources (pinned in config/dex_programs.toml):
//   Raydium AMM v4  raydium-io/raydium-amm@91739d8 program/src/state.rs (AmmInfo, Fees, StateData)
//   Whirlpool       orca-so/whirlpools@cc683ec programs/whirlpool/src/state/{whirlpool,fixed_tick_array,dynamic_tick_array,tick}.rs
//   SPL Token       spl-token Account/Mint pack layouts; Token-2022 TLV extensions (solana-program/token-2022@f052d0a)
//   Kamino Reserve  Kamino-Finance/klend@77e8185 libs/klend-interface/src/state/reserve.rs (repr(C), 8616 bytes + 8)
#include <array>
#include <cstddef>
#include <cstdint>
#include <vector>

#include "core/bytes.hpp"
#include "core/error.hpp"
#include "core/types.hpp"

namespace arb::state {

// --- SPL Token / Token-2022 --------------------------------------------------

constexpr size_t TOKEN_ACCOUNT_LEN = 165;
constexpr size_t MINT_LEN = 82;

struct TokenAccount {
  Pubkey mint{}, owner{};
  uint64_t amount = 0;
  bool has_delegate = false;
  uint8_t state = 0;  // 0 uninitialized, 1 initialized, 2 frozen
  bool is_native = false;
  bool has_close_authority = false;
  Pubkey token_program{};
};

/** Token-2022 extension type numbers (ExtensionType in token-2022 program/src/extension/mod.rs). */
namespace ext {
enum : uint16_t {
  TransferFeeConfig = 1,
  MintCloseAuthority = 3,
  ConfidentialTransferMint = 4,
  DefaultAccountState = 6,
  NonTransferable = 9,
  InterestBearingConfig = 10,
  PermanentDelegate = 12,
  TransferHook = 14,
  ConfidentialTransferFeeConfig = 16,
  MetadataPointer = 18,
  TokenMetadata = 19,
  ScaledUiAmount = 25,
  Pausable = 26,
};
}

struct TransferFee {
  uint64_t epoch = 0;
  uint64_t maximum_fee = 0;
  uint16_t basis_points = 0;
};

struct Mint {
  uint8_t decimals = 0;
  uint64_t supply = 0;
  bool has_mint_authority = false;
  bool has_freeze_authority = false;
  Pubkey freeze_authority{};
  Pubkey token_program{};
  uint64_t ext_mask = 0;  // bit n = extension type n present (n < 64)
  uint16_t ext_types[24]{};
  uint8_t n_ext = 0;
  bool has_transfer_fee = false;
  TransferFee older_fee, newer_fee;
  uint8_t default_account_state = 0;  // DefaultAccountState extension value (2 = frozen)

  bool has_ext(uint16_t t) const { return t < 64 && (ext_mask >> t) & 1; }
  /** TransferFeeConfig::get_epoch_fee. */
  const TransferFee& epoch_fee(uint64_t epoch) const { return epoch >= newer_fee.epoch ? newer_fee : older_fee; }
};

/** TransferFee::calculate_fee: ceil(amount * bps / 10000), capped at maximum_fee. */
uint64_t transfer_fee(const TransferFee& f, uint64_t amount);

Result<TokenAccount> decode_token_account(const uint8_t* d, size_t n, const Pubkey& owner);
Result<Mint> decode_mint(const uint8_t* d, size_t n, const Pubkey& owner);

// --- Raydium AMM v4 ------------------------------------------------------------

constexpr size_t RAYDIUM_V4_LEN = 752;

struct RaydiumV4 {
  uint64_t status = 0, nonce = 0;
  uint64_t coin_decimals = 0, pc_decimals = 0;
  uint64_t trade_fee_numerator = 0, trade_fee_denominator = 0;
  uint64_t swap_fee_numerator = 0, swap_fee_denominator = 0;
  uint64_t need_take_pnl_coin = 0, need_take_pnl_pc = 0;
  uint64_t pool_open_time = 0;
  Pubkey coin_vault{}, pc_vault{}, coin_mint{}, pc_mint{}, lp_mint{};
  Pubkey open_orders{}, market{}, market_program{}, target_orders{}, amm_owner{};
  uint64_t lp_amount = 0;

  /** AmmStatus::swap_permission (Initialized, SwapOnly, WaitingTrade). */
  bool swap_permitted() const { return status == 1 || status == 6 || status == 7; }
};

Result<RaydiumV4> decode_raydium_v4(const uint8_t* d, size_t n, const Pubkey& owner);

// --- Orca Whirlpool --------------------------------------------------------------

constexpr size_t WHIRLPOOL_LEN = 653;
constexpr int32_t TICK_ARRAY_SIZE = 88;
constexpr size_t FIXED_TICK_ARRAY_LEN = 8 + 4 + 113 * 88 + 32;  // 9988
constexpr size_t DYNAMIC_TICK_ARRAY_MIN_LEN = 8 + 4 + 32 + 16 + 88;
constexpr int32_t MIN_TICK_INDEX = -443636;
constexpr int32_t MAX_TICK_INDEX = 443636;

inline constexpr uint8_t DISC_WHIRLPOOL[8] = {63, 149, 209, 12, 225, 128, 99, 9};
inline constexpr uint8_t DISC_FIXED_TICK_ARRAY[8] = {69, 97, 189, 190, 110, 7, 66, 187};
inline constexpr uint8_t DISC_DYNAMIC_TICK_ARRAY[8] = {17, 216, 246, 142, 225, 199, 218, 56};

struct Whirlpool {
  Pubkey whirlpools_config{};
  uint16_t tick_spacing = 0;
  uint16_t fee_tier_index = 0;
  uint16_t fee_rate = 0;           // hundredths of a bp
  uint16_t protocol_fee_rate = 0;  // bp of the fee
  u128 liquidity = 0;
  u128 sqrt_price = 0;  // Q64.64
  int32_t tick_current_index = 0;
  uint64_t protocol_fee_owed_a = 0, protocol_fee_owed_b = 0;
  Pubkey token_mint_a{}, token_vault_a{}, token_mint_b{}, token_vault_b{};
  u128 fee_growth_global_a = 0, fee_growth_global_b = 0;

  /** Whirlpool::is_initialized_with_adaptive_fee_tier: such pools price through an Oracle account. */
  bool adaptive_fee() const { return fee_tier_index != tick_spacing; }
};

struct Tick {
  bool initialized = false;
  i128 liquidity_net = 0;
  u128 liquidity_gross = 0;
};

/** Fixed and dynamic tick arrays normalized to one in-memory form. */
struct TickArray {
  int32_t start_tick_index = 0;
  Pubkey whirlpool{};
  bool dynamic = false;
  std::array<Tick, TICK_ARRAY_SIZE> ticks{};
};

Result<Whirlpool> decode_whirlpool(const uint8_t* d, size_t n, const Pubkey& owner);
Result<TickArray> decode_tick_array(const uint8_t* d, size_t n, const Pubkey& owner);

// --- Raydium CPMM (raydium-cp-swap, programs/cp-swap/src/states/{pool,config}.rs) ---------

constexpr size_t CPMM_POOL_LEN = 637;
constexpr size_t CPMM_CONFIG_LEN = 236;
inline constexpr uint8_t DISC_CPMM_POOL[8] = {247, 237, 227, 245, 215, 195, 222, 70};    // account:PoolState
inline constexpr uint8_t DISC_CPMM_CONFIG[8] = {218, 244, 33, 104, 203, 203, 43, 111};  // account:AmmConfig

struct CpmmPool {
  Pubkey amm_config{}, token_0_vault{}, token_1_vault{}, token_0_mint{}, token_1_mint{};
  Pubkey token_0_program{}, token_1_program{}, observation_key{};
  uint8_t auth_bump = 0, status = 0;
  uint64_t protocol_fees_0 = 0, protocol_fees_1 = 0, fund_fees_0 = 0, fund_fees_1 = 0;
  uint64_t open_time = 0;
  uint8_t creator_fee_on = 0;  // 0 both (on input), 1 only token 0, 2 only token 1
  bool enable_creator_fee = false;
  uint64_t creator_fees_0 = 0, creator_fees_1 = 0;

  /** status bit 2 set = swap disabled. */
  bool swap_enabled() const { return (status & 4) == 0; }
  /** Fees owed to protocol, fund and creator sit in the vaults but are not reserves. */
  uint64_t fees_owed_0() const { return protocol_fees_0 + fund_fees_0 + creator_fees_0; }
  uint64_t fees_owed_1() const { return protocol_fees_1 + fund_fees_1 + creator_fees_1; }
  /** PoolState::is_creator_fee_on_input. */
  bool creator_fee_on_input(bool zero_for_one) const {
    return creator_fee_on == 0 || (creator_fee_on == 1 && zero_for_one) || (creator_fee_on == 2 && !zero_for_one);
  }
};

/** Fee rates in hundredths of a bp (1e-6). */
struct CpmmConfig {
  uint64_t trade_fee_rate = 0, protocol_fee_rate = 0, fund_fee_rate = 0, creator_fee_rate = 0;
};

Result<CpmmPool> decode_cpmm_pool(const uint8_t* d, size_t n, const Pubkey& owner);
Result<CpmmConfig> decode_cpmm_config(const uint8_t* d, size_t n, const Pubkey& owner);

// --- Raydium CLMM (raydium-clmm@6d7a0f8, programs/amm/src/states/{pool,config,tick_array}.rs) -------------

constexpr size_t CLMM_POOL_LEN = 1544;
constexpr size_t CLMM_CONFIG_LEN = 117;
constexpr size_t CLMM_TICK_ARRAY_LEN = 10240;
constexpr int32_t CLMM_TICK_ARRAY_SIZE = 60;
inline constexpr uint8_t DISC_CLMM_POOL[8] = {247, 237, 227, 245, 215, 195, 222, 70};        // account:PoolState
inline constexpr uint8_t DISC_CLMM_CONFIG[8] = {218, 244, 33, 104, 203, 203, 43, 111};      // account:AmmConfig
inline constexpr uint8_t DISC_CLMM_TICK_ARRAY[8] = {192, 155, 85, 205, 49, 249, 129, 42};   // account:TickArrayState

struct ClmmPool {
  Pubkey amm_config{}, token_mint_0{}, token_mint_1{}, token_vault_0{}, token_vault_1{}, observation_key{};
  uint16_t tick_spacing = 0;
  u128 liquidity = 0, sqrt_price_x64 = 0;
  int32_t tick_current = 0;
  uint8_t status = 0, fee_on = 0;  // fee_on: 0 from input, 1 token 0 only, 2 token 1 only
  uint64_t tick_array_bitmap[16]{};
  uint64_t open_time = 0;
  bool dynamic_fee = false;  // DynamicFeeInfo set: the fee moves with the block time (not modelled)

  /** PoolStatusBitIndex::Swap (bit 4) set = swap disabled. */
  bool swap_enabled() const { return (status & 0x10) == 0; }
  /** PoolState::is_fee_on_input. */
  bool fee_on_input(bool zero_for_one) const { return fee_on == 1 ? zero_for_one : fee_on == 2 ? !zero_for_one : true; }
};

/** Fee rates in hundredths of a bp (1e-6). */
struct ClmmConfig {
  uint32_t protocol_fee_rate = 0, trade_fee_rate = 0, fund_fee_rate = 0;
  uint16_t tick_spacing = 0;
};

struct ClmmTick {
  int32_t tick = 0;
  i128 liquidity_net = 0;
  u128 liquidity_gross = 0;
  bool limit_orders = false;  // orders_amount or part_filled_orders_remaining > 0 (matching is not modelled)
  bool initialized() const { return liquidity_gross > 0 || limit_orders; }
};

struct ClmmTickArray {
  Pubkey pool{};
  int32_t start_tick_index = 0;
  std::array<ClmmTick, CLMM_TICK_ARRAY_SIZE> ticks{};
};

Result<ClmmPool> decode_clmm_pool(const uint8_t* d, size_t n, const Pubkey& owner);
Result<ClmmConfig> decode_clmm_config(const uint8_t* d, size_t n, const Pubkey& owner);
Result<ClmmTickArray> decode_clmm_tick_array(const uint8_t* d, size_t n, const Pubkey& owner);
/** Tick-array PDA ["tick_array", pool, start_index (i32 big-endian)]. */
Pubkey clmm_tick_array_pda(const Pubkey& pool, int32_t start);

// --- PumpSwap (pump-fun/pump-public-docs@639b87f idl/pump_amm.json, idl/pump_fees.json) -----------------------
// Accounts grew by appended fields; a shorter (older) account reads the missing trailing fields as 0 / false.

constexpr size_t PUMP_POOL_MIN_LEN = 211;  // through lp_supply
inline constexpr uint8_t DISC_PUMP_POOL[8] = {241, 154, 109, 4, 17, 177, 109, 188};
inline constexpr uint8_t DISC_PUMP_GLOBAL_CONFIG[8] = {149, 8, 156, 202, 160, 252, 176, 217};
inline constexpr uint8_t DISC_PUMP_FEE_CONFIG[8] = {143, 52, 146, 187, 219, 123, 76, 155};

struct PumpPool {
  uint16_t index = 0;
  Pubkey creator{}, base_mint{}, quote_mint{}, pool_base_token_account{}, pool_quote_token_account{};
  Pubkey coin_creator{};
  bool is_mayhem_mode = false, is_cashback_coin = false;
  i128 virtual_quote_reserves = 0;  // added to the quote vault for pricing; may be negative
  uint64_t creator_fee_bps = 0;     // the pool's own creator fee (custom quote pairs); 0 = the schedule's
  uint64_t protocol_fees = 0, creator_fees = 0;  // v2 fee buckets kept in the quote vault
};

struct PumpGlobalConfig {
  uint8_t disable_flags = 0;           // bit 3 buy, bit 4 sell
  bool creator_fee_configurable = false;
  Pubkey buyback_fee_recipients[8]{};  // wallets; the v2 trade takes one's quote-mint ATA
};

struct PumpFees {
  uint64_t lp_fee_bps = 0, protocol_fee_bps = 0, creator_fee_bps = 0;
};
struct PumpFeeTier {
  u128 market_cap_lamports_threshold = 0;
  PumpFees fees;
};
struct PumpFeeConfig {
  PumpFees flat_fees, exotic_flat_fees;
  std::vector<PumpFeeTier> fee_tiers, stable_fee_tiers;
};

Result<PumpPool> decode_pump_pool(const uint8_t* d, size_t n, const Pubkey& owner);
Result<PumpGlobalConfig> decode_pump_global_config(const uint8_t* d, size_t n, const Pubkey& owner);
Result<PumpFeeConfig> decode_pump_fee_config(const uint8_t* d, size_t n, const Pubkey& owner);

// --- Kamino Lend reserve -----------------------------------------------------------

constexpr size_t KAMINO_RESERVE_LEN = 8 + 8616;
inline constexpr uint8_t DISC_KAMINO_RESERVE[8] = {43, 242, 204, 202, 26, 247, 59, 127};

struct KaminoReserve {
  Pubkey lending_market{};
  Pubkey liquidity_mint{}, supply_vault{}, fee_vault{}, token_program{};
  uint64_t available_amount = 0;
  uint64_t mint_decimals = 0;
  uint64_t flash_loan_fee_sf = 0;  // u64::MAX = flash loans disabled
  uint8_t status = 0;              // 0 active, 1 obsolete, 2 hidden
  uint64_t last_update_slot = 0;

  bool flash_enabled() const { return flash_loan_fee_sf != UINT64_MAX && status == 0; }
};

Result<KaminoReserve> decode_kamino_reserve(const uint8_t* d, size_t n, const Pubkey& owner);

}  // namespace arb::state
