// PumpSwap (pump AMM) quotes, ported from pump-rust-client 0.4.1 (pump-fun's official client):
//   src/math/amm.rs   effective_quote_reserve, real_quote_reserve, check_trading_enabled, sell_base_input,
//                     buy_quote_input, buy_base_input
//   src/math/fees.rs  fee_amount (ceil), creator_fee_amount, exact_in_fee_amounts, pool_market_cap,
//                     calculate_fee_tier, fees_for_quote_mint, compute_amm_fee_bps
// The v2 trades (sell_v2, buy_exact_quote_in_v2) price exactly like v1 (pump-public-docs PUMP_SWAP_TRADE_V2.md).
// Token "a" is the pool's base mint, "b" its quote mint.
#include "core/programs.hpp"
#include "dex/quoter.hpp"

namespace arb::dex {

namespace {

constexpr u128 BPS = 10'000;
inline constexpr Pubkey TOKEN_2022_NATIVE_MINT = PK("9pan9bMn5HatX4EJdBwg9VgCa7Uz5HL8N1m5D3NdXejP");

u128 fee_amount(u128 amount, uint64_t bps) { return (amount * bps + BPS - 1) / BPS; }

bool sol_like(const Pubkey& m) { return is_zero(m) || m == mint::WSOL || m == TOKEN_2022_NATIVE_MINT; }

const state::PumpFees* tier_fees(const std::vector<state::PumpFeeTier>& tiers, u128 market_cap) {
  if (tiers.empty()) return nullptr;
  if (market_cap < tiers.front().market_cap_lamports_threshold) return &tiers.front().fees;
  for (auto it = tiers.rbegin(); it != tiers.rend(); ++it)
    if (market_cap >= it->market_cap_lamports_threshold) return &it->fees;
  return &tiers.front().fees;
}

bool charges_transfer_fee(const std::shared_ptr<const state::Mint>& m) {
  return m && m->has_transfer_fee;
}

}  // namespace

Result<PumpFeeBps> pump_fee_bps(const PoolState& p, uint64_t base_reserve, uint64_t quote_reserve) {
  if (!p.pump_fees) return fail(Errc::not_quotable, "no fee config");
  if (base_reserve == 0) return fail(Errc::div_by_zero, "empty base reserve");
  const state::PumpFeeConfig& c = *p.pump_fees;
  const state::PumpFees* f = nullptr;
  if (!p.pump_canonical) {
    f = &c.flat_fees;
  } else {
    uint64_t supply = p.mint_a_info ? p.mint_a_info->supply : 0;
    u128 market_cap = u128(quote_reserve) * u128(supply) / u128(base_reserve);
    if (sol_like(p.mint_b)) f = tier_fees(c.fee_tiers, market_cap);
    else if (p.mint_b == mint::USDC) f = tier_fees(c.stable_fee_tiers, market_cap);
    else {
      const state::PumpFees& ex = c.exotic_flat_fees;
      f = ex.lp_fee_bps == 0 && ex.protocol_fee_bps == 0 && ex.creator_fee_bps == 0 ? &c.flat_fees : &ex;
    }
    if (!f) return fail(Errc::bad_data, "EmptyFeeTiers");
  }
  PumpFeeBps out;
  out.lp = f->lp_fee_bps;
  out.protocol = f->protocol_fee_bps;
  // configured_creator_fee_bps: the pool's own rate when the gate is on and it is set.
  out.creator = p.pump_cfg.creator_fee_configurable && p.pump.creator_fee_bps != 0 ? p.pump.creator_fee_bps : f->creator_fee_bps;
  return out;
}

bool PumpSwapQuoter::supports(const PoolState& p) const {
  return p.kind == DexKind::PumpSwap && !p.pump.is_cashback_coin && !p.pump.is_mayhem_mode && !charges_transfer_fee(p.mint_a_info) &&
         !charges_transfer_fee(p.mint_b_info);
}

Result<Quote> PumpSwapQuoter::quote_exact_in(const PoolState& p, bool a_to_b, uint64_t amount_in) const {
  if (p.kind != DexKind::PumpSwap) return fail(Errc::unsupported, "not a pumpswap pool");
  if (!p.complete) return fail(Errc::not_quotable, "incomplete state");
  if (!supports(p)) return fail(Errc::unsupported, "cashback, mayhem or transfer-fee pool");
  if (amount_in == 0) return fail(Errc::zero_amount, "amount_in 0");
  const bool is_buy = !a_to_b;
  if (p.pump_cfg.disable_flags & (1u << (is_buy ? 3 : 4))) return fail(Errc::unsupported, "TradingDisabled");
  const uint64_t base_reserve = p.vault_a_amount;
  i128 eq = i128(p.vault_b_amount) + p.pump.virtual_quote_reserves;
  if (eq < 0 || eq > i128(UINT64_MAX)) return fail(Errc::overflow, "effective quote reserve");
  const uint64_t quote_reserve = uint64_t(eq);
  if (base_reserve == 0 || quote_reserve == 0) return fail(Errc::insufficient_liquidity, "EmptyReserves");
  auto bps = pump_fee_bps(p, base_reserve, quote_reserve);
  if (!bps) return fail(bps.error().code, bps.error().what);
  const bool has_creator = !is_zero(p.pump.coin_creator);
  const uint64_t creator_bps = has_creator ? bps->creator : 0;
  Quote q;
  q.est_compute_units = cu_;
  if (!is_buy) {
    // sell_base_input: raw = quote * in / (base + in); every fee is ceil(raw * bps / 1e4) and comes off the output.
    u128 raw = u128(quote_reserve) * amount_in / (u128(base_reserve) + amount_in);
    u128 lp = fee_amount(raw, bps->lp), protocol = fee_amount(raw, bps->protocol), creator = has_creator ? fee_amount(raw, bps->creator) : 0;
    u128 total = lp + protocol + creator;
    if (raw < total) return fail(Errc::zero_amount, "FeesExceedOutput");
    // The vault must hold what leaves it (raw less the LP fee, which stays) beyond the v2 fee buckets.
    uint64_t buckets = p.pump.protocol_fees + p.pump.creator_fees;
    if (buckets > p.vault_b_amount || u128(p.vault_b_amount - buckets) < raw - lp)
      return fail(Errc::insufficient_liquidity, "InsufficientRealQuoteReserves");
    if (raw - total == 0) return fail(Errc::zero_amount, "zero quote out");
    q.amount_in = amount_in;
    q.amount_out = uint64_t(raw - total);
    q.fee_paid = uint64_t(total);
    return q;
  }
  // buy_quote_input: net so that net + fees(net) <= spendable, then base out on net - 1.
  const uint64_t b3[3] = {bps->lp, bps->protocol, creator_bps};
  u128 total_bps = u128(b3[0]) + b3[1] + b3[2];
  u128 input = amount_in;
  u128 net = input * BPS / (BPS + total_bps);
  if (net == 0) return fail(Errc::zero_amount, "ZeroAmount");
  u128 cost = net;
  for (uint64_t b : b3) cost += fee_amount(net, b);
  if (cost > input) {
    if (net <= cost - input) return fail(Errc::zero_amount, "ZeroAmount");
    net -= cost - input;
  }
  if (net < 2) return fail(Errc::zero_amount, "ZeroAmount");
  u128 base_out = (net - 1) * u128(base_reserve) / (u128(quote_reserve) + (net - 1));
  if (base_out == 0) return fail(Errc::zero_amount, "ZeroAmount");
  if (base_out >= base_reserve) return fail(Errc::insufficient_liquidity, "BaseOutExceedsReserve");
  // The program then buys exactly base_out: raw = ceil(quote * out / (base - out)), plus each fee on raw.
  u128 raw = (u128(quote_reserve) * base_out + (u128(base_reserve) - base_out) - 1) / (u128(base_reserve) - base_out);
  u128 charged = raw + fee_amount(raw, b3[0]) + fee_amount(raw, b3[1]) + (has_creator ? fee_amount(raw, b3[2]) : 0);
  if (charged > input) return fail(Errc::overflow, "ExceededSlippage");
  q.amount_in = uint64_t(charged);
  q.amount_out = uint64_t(base_out);
  q.fee_paid = uint64_t(charged - raw);
  return q;
}

}  // namespace arb::dex
