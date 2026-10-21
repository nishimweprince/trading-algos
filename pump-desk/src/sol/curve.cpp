#include "sol/curve.hpp"

#include <algorithm>

#include "sol/constants.hpp"
#include "sol/crypto.hpp"

namespace pd {

namespace {

u128 ceil_div(u128 a, u128 b) { return (a + b - 1) / b; }

void budget(IxList& l, const TxParams& p) {
  if (p.cu_limit) ix::cu_limit(l, p.cu_limit);
  if (p.cu_price) ix::cu_price(l, p.cu_price);
}

void tips(IxList& l, const Pubkey& user, const TxParams& p) {
  for (uint8_t i = 0; i < p.n_tips; ++i)
    if (p.tips[i].lamports) ix::transfer(l, user, p.tips[i].account, p.tips[i].lamports);
}

/** The first set entry at or after `start` (the SDK skips default keys). */
const Pubkey& pick(const Pubkey (&list)[8], uint8_t start) {
  for (int i = 0; i < 8; ++i) {
    const Pubkey& k = list[(start + i) & 7];
    if (!is_zero(k)) return k;
  }
  return list[0];
}

/** Accounts 0..11 shared by buy and sell up to the program id; buy and sell order 8/9 differently. */
void head_accounts(Ix& x, const Pubkey& user, const CurveKeys& k, const AmmConfig& cfg, const TxParams& p) {
  const Derived& d = derived();
  const Pubkey& fee_recipient = pick(k.mayhem ? cfg.curve_reserved_recipients : cfg.curve_recipients,
                                     p.fee_recipient_index);
  x.ro(d.pump_global)
      .rw(fee_recipient)
      .ro(k.mint)
      .rw(k.bonding_curve)
      .rw(k.associated_bonding_curve)
      .rw(k.user_ata)
      .acc(user, true, true)
      .ro(prog::SYSTEM);
}

}  // namespace

void set_curve_creator(CurveKeys& k, const Pubkey& creator) {
  k.creator = creator;
  k.creator_vault = pda({std::string_view("creator-vault"), creator}, prog::PUMP_FUN);
}

void derive_curve_keys(const Pubkey& user, const Pubkey& mint, const Pubkey& token_program, const Pubkey& creator,
                       bool mayhem, bool cashback, CurveKeys& out) {
  out.mint = mint;
  out.token_program = token_program;
  out.bonding_curve = pda({std::string_view("bonding-curve"), mint}, prog::PUMP_FUN);
  out.associated_bonding_curve = ata(out.bonding_curve, mint, token_program);
  out.bonding_curve_v2 = pda({std::string_view("bonding-curve-v2"), mint}, prog::PUMP_FUN);
  out.user_ata = ata(user, mint, token_program);
  out.user_volume_acc = pda({std::string_view("user_volume_accumulator"), user}, prog::PUMP_FUN);
  out.mayhem = mayhem;
  out.cashback = cashback;
  set_curve_creator(out, creator);
}

uint64_t curve_fee_bps(const AmmConfig& cfg, const Curve& c, int fallback_bps) {
  uint64_t fallback = fallback_bps < 0 ? 0 : uint64_t(fallback_bps);
  if (!cfg.curve_valid) return fallback;
  uint64_t creator = c.creator_fee_bps ? c.creator_fee_bps : cfg.curve_creator_bps;
  if (c.has_creator && is_zero(c.creator)) creator = 0;
  // The fee program's market-cap tiers can only lower the global rate in practice;
  // pricing on the larger of the two keeps a buy inside its budget.
  return std::max(fallback, cfg.curve_protocol_bps + creator);
}

uint64_t curve_buy_tokens(const Curve& c, uint64_t spendable, uint64_t fee_bps) {
  if (c.complete || c.virtual_token == 0 || c.real_token == 0 || spendable < 2) return 0;
  u128 net = u128(spendable) * 10000 / (10000 + fee_bps);
  u128 fees = ceil_div(net * fee_bps, 10000);
  if (net + fees > spendable) net -= net + fees - spendable;
  if (net < 2) return 0;
  u128 tokens = (net - 1) * c.virtual_token / (u128(c.virtual_sol) + net - 1);
  return uint64_t(std::min<u128>(tokens, c.real_token));
}

uint64_t curve_buy_cost(const Curve& c, uint64_t tokens, uint64_t fee_bps) {
  if (tokens == 0 || tokens >= c.virtual_token) return 0;
  u128 net = ceil_div(u128(tokens) * c.virtual_sol, u128(c.virtual_token - tokens)) + 1;
  return uint64_t(ceil_div(net * (10000 + fee_bps), 10000));
}

uint64_t curve_sell_proceeds(const Curve& c, uint64_t tokens, uint64_t fee_bps) {
  if (tokens == 0 || c.virtual_token == 0) return 0;
  u128 sol = u128(tokens) * c.virtual_sol / (u128(c.virtual_token) + tokens);
  u128 fee = ceil_div(sol * fee_bps, 10000);
  return sol > fee ? uint64_t(sol - fee) : 0;
}

void curve_buy_ix(IxList& l, const Pubkey& user, const CurveKeys& k, const AmmConfig& cfg, const TxParams& p,
                  uint64_t amount, uint64_t max_sol_cost, bool partial_fill) {
  const Derived& d = derived();
  Ix& x = l.add(prog::PUMP_FUN);
  head_accounts(x, user, k, cfg, p);
  x.ro(k.token_program)
      .rw(k.creator_vault)
      .ro(d.pump_event_authority)
      .ro(prog::PUMP_FUN)
      .ro(d.pump_global_volume_acc)
      .rw(k.user_volume_acc)
      .ro(d.pump_fee_config)
      .ro(prog::PUMP_FEE);
  x.ro(k.bonding_curve_v2).rw(pick(cfg.curve_buyback_recipients, p.buyback_index));
  // buy(amount u64, max_sol_cost u64, track_volume OptionBool(true), partial_fill OptionBool)
  x.bytes(disc::BUY, 8).u64(amount).u64(max_sol_cost).u8(1).u8(partial_fill ? 1 : 0);
}

void curve_sell_ix(IxList& l, const Pubkey& user, const CurveKeys& k, const AmmConfig& cfg, const TxParams& p,
                   uint64_t amount, uint64_t min_sol_output) {
  const Derived& d = derived();
  Ix& x = l.add(prog::PUMP_FUN);
  head_accounts(x, user, k, cfg, p);
  x.rw(k.creator_vault)
      .ro(k.token_program)
      .ro(d.pump_event_authority)
      .ro(prog::PUMP_FUN)
      .ro(d.pump_fee_config)
      .ro(prog::PUMP_FEE);
  if (k.cashback) x.rw(k.user_volume_acc);
  x.ro(k.bonding_curve_v2).rw(pick(cfg.curve_buyback_recipients, p.buyback_index));
  // sell(amount u64, min_sol_output u64)
  x.bytes(disc::SELL, 8).u64(amount).u64(min_sol_output);
}

void curve_buy_ixs(IxList& l, const Pubkey& user, const CurveKeys& k, const AmmConfig& cfg, const TxParams& p,
                   uint64_t amount, uint64_t max_sol_cost, bool partial_fill) {
  l.clear();
  budget(l, p);
  ix::ata_idempotent(l, user, k.user_ata, user, k.mint, k.token_program);
  curve_buy_ix(l, user, k, cfg, p, amount, max_sol_cost, partial_fill);
  tips(l, user, p);
}

void curve_sell_ixs(IxList& l, const Pubkey& user, const CurveKeys& k, const AmmConfig& cfg, const TxParams& p,
                    uint64_t amount, uint64_t min_sol_output, bool close_base) {
  l.clear();
  budget(l, p);
  curve_sell_ix(l, user, k, cfg, p, amount, min_sol_output);
  if (close_base) ix::close_account(l, k.user_ata, user, user, k.token_program);
  tips(l, user, p);
}

}  // namespace pd
