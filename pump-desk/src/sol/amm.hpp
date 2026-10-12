#pragma once
// PumpSwap GlobalConfig / FeeConfig and the SDK's quote math
// (@pump-fun/pump-swap-sdk 1.20.0: computeFeesBps, buyQuoteInput, sellBaseInput),
// ported to 128-bit integers so results match BN to the lamport.
#include <cstddef>
#include <cstdint>

#include "core/bytes.hpp"
#include "core/types.hpp"

namespace pd {

struct Fees {
  uint64_t lp = 0, protocol = 0, creator = 0;
};

struct FeeTier {
  u128 threshold = 0;  // market cap, lamports
  Fees fees;
};

constexpr size_t MAX_FEE_TIERS = 32;

/** Decoded GlobalConfig + FeeConfig, plus pre-derived WSOL ATAs of every fee recipient. POD (seqlocked). */
struct AmmConfig {
  bool valid = false;
  uint64_t lp_bps = 0, protocol_bps = 0, coin_creator_bps = 0;
  uint8_t disable_flags = 0;  // bit 3 = buy disabled, bit 4 = sell disabled
  bool creator_fee_configurable = false;
  Pubkey protocol_recipients[8]{};
  Pubkey protocol_recipient_atas[8]{};
  Pubkey reserved_recipients[8]{};  // reserved_fee_recipient + reserved_fee_recipients[7] (mayhem)
  Pubkey reserved_recipient_atas[8]{};
  Pubkey buyback_recipients[8]{};
  Pubkey buyback_recipient_atas[8]{};
  bool has_fee_config = false;
  Fees flat;
  uint32_t n_tiers = 0;
  FeeTier tiers[MAX_FEE_TIERS];
};

/** Fills the GlobalConfig half (recipient ATAs included). False on a bad account. */
bool decode_global_config(const uint8_t* d, size_t n, AmmConfig& c);
/** Fills the FeeConfig half. False on a bad account. */
bool decode_fee_config(const uint8_t* d, size_t n, AmmConfig& c);

struct FeeBps {
  uint64_t lp = 0, protocol = 0, creator = 0;
};

constexpr uint64_t PUMP_AMM_TOTAL_SUPPLY = 1'000'000'000'000'000ULL;

/**
 * computeFeesBps for a canonical (pump) pool quoted in WSOL. `effective_quote`
 * is vault + virtual. `pool_creator_fee_bps` overrides the creator fee when
 * the config allows it.
 */
FeeBps compute_fees_bps(const AmmConfig& cfg, bool is_pump_pool, uint64_t mint_supply, uint64_t base_reserve,
                        uint64_t effective_quote, bool mayhem, uint64_t pool_creator_fee_bps);

struct BuyQuote {
  bool ok = false;
  uint64_t base_out = 0;   // base_amount_out
  uint64_t max_quote = 0;  // max_quote_amount_in (quote * (1 + slippage))
};

/** buyQuoteInput: spend `quote` lamports; `slippage_pct` as in the SDK (e.g. 30 = 30 %). */
BuyQuote buy_quote_input(uint64_t quote, double slippage_pct, uint64_t base_reserve, uint64_t quote_reserve,
                         uint64_t virtual_quote, const FeeBps& fees, bool coin_creator_is_default);

struct SellQuote {
  bool ok = false;
  uint64_t final_quote = 0;  // proceeds after fees
  uint64_t min_quote = 0;    // min_quote_amount_out
};

/** sellBaseInput: sell `base` units. Fails when the real vault can't cover the payout. */
SellQuote sell_base_input(uint64_t base, double slippage_pct, uint64_t base_reserve, uint64_t quote_reserve,
                          uint64_t virtual_quote, const FeeBps& fees, bool coin_creator_is_default);

/** Mid price in SOL per whole token, priced on vault + virtual quote. */
inline double pool_price_sol(uint64_t base_reserve, uint64_t quote_reserve, uint64_t virtual_quote) {
  if (base_reserve == 0) return 0.0;
  return (double(quote_reserve) + double(virtual_quote)) / 1e9 / (double(base_reserve) / 1e6);
}

}  // namespace pd
