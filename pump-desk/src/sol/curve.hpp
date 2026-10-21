#pragma once
// pump.fun bonding-curve buy / sell transactions, mirroring @pump-fun/pump-sdk 4.2.0
// PumpSdk.buyInstructions / sellInstructions (getBuyInstructionInternal /
// getSellInstructionInternal), with partial_fill set on buys so a buy that meets
// fewer remaining tokens than it asked for fills the rest instead of failing 6021:
//   buy : [CU limit, CU price] ata(base) buy [tips]
//   sell: [CU limit, CU price] sell [close(base) on a full exit] [tips]
// Native SOL moves directly: no WSOL account on either side.
#include <cstdint>

#include "core/types.hpp"
#include "sol/amm.hpp"
#include "sol/layouts.hpp"
#include "sol/swap.hpp"
#include "sol/tx.hpp"

namespace pd {

/** Per-mint keys of a bonding curve, derived once (at admission) off the hot path. */
struct CurveKeys {
  Pubkey mint{}, token_program{};
  Pubkey bonding_curve{}, associated_bonding_curve{}, bonding_curve_v2{};
  Pubkey creator{}, creator_vault{};
  Pubkey user_ata{}, user_volume_acc{};
  bool mayhem = false, cashback = false;
};

void derive_curve_keys(const Pubkey& user, const Pubkey& mint, const Pubkey& token_program, const Pubkey& creator,
                       bool mayhem, bool cashback, CurveKeys& out);
/** Re-derive the creator half (the curve account named a different creator). */
void set_curve_creator(CurveKeys& k, const Pubkey& creator);

/** Fee rate on a curve trade: the curve's own creator fee when set, else the global schedule. */
uint64_t curve_fee_bps(const AmmConfig& cfg, const Curve& c, int fallback_bps);

/** Tokens `spendable` lamports buy (pump IDL buy_exact_sol_in quote), capped at the real reserve. */
uint64_t curve_buy_tokens(const Curve& c, uint64_t spendable, uint64_t fee_bps);
/** Lamports (fee included) to buy `tokens` (IDL reverse quote). */
uint64_t curve_buy_cost(const Curve& c, uint64_t tokens, uint64_t fee_bps);
/** Lamports out, after fees, for selling `tokens` (SDK getSellSolAmountFromTokenAmount). */
uint64_t curve_sell_proceeds(const Curve& c, uint64_t tokens, uint64_t fee_bps);
/** SOL per whole token at the curve's virtual reserves. */
inline double curve_price_sol(const Curve& c) {
  if (c.virtual_token == 0) return 0.0;
  return (double(c.virtual_sol) / 1e9) / (double(c.virtual_token) / 1e6);
}
/**
 * Overhang: the price drop (%) if `holder_tokens` were sold on the curve, 1 - (vtok / (vtok + H))^2
 * (fees ignored). Same formula as research/replay_curve.py overhang_drop_pct.
 */
inline double overhang_drop_pct(const Curve& c, uint64_t holder_tokens) {
  if (c.virtual_token == 0) return 0.0;
  double r = double(c.virtual_token) / (double(c.virtual_token) + double(holder_tokens));
  return 100.0 * (1.0 - r * r);
}

/** Append the pump.fun `buy` instruction (+ bonding_curve_v2, buyback recipient). */
void curve_buy_ix(IxList& l, const Pubkey& user, const CurveKeys& k, const AmmConfig& cfg, const TxParams& p,
                  uint64_t amount, uint64_t max_sol_cost, bool partial_fill);
/** Append the pump.fun `sell` instruction (+ user volume accumulator on cashback coins). */
void curve_sell_ix(IxList& l, const Pubkey& user, const CurveKeys& k, const AmmConfig& cfg, const TxParams& p,
                   uint64_t amount, uint64_t min_sol_output);

/** Full buy instruction list (see header). */
void curve_buy_ixs(IxList& l, const Pubkey& user, const CurveKeys& k, const AmmConfig& cfg, const TxParams& p,
                   uint64_t amount, uint64_t max_sol_cost, bool partial_fill = true);
/** Full sell instruction list; `close_base` also closes the emptied base ATA (rent back). */
void curve_sell_ixs(IxList& l, const Pubkey& user, const CurveKeys& k, const AmmConfig& cfg, const TxParams& p,
                    uint64_t amount, uint64_t min_sol_output, bool close_base);

}  // namespace pd
