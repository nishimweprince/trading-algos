#include "sol/swap.hpp"

#include "sol/constants.hpp"
#include "sol/crypto.hpp"

namespace pd {

void derive_user_keys(const Pubkey& user, UserKeys& out) {
  out.user = user;
  out.wsol_ata = ata(user, prog::WSOL, prog::TOKEN);
  out.volume_acc = pda({std::string_view("user_volume_accumulator"), user}, prog::PUMP_SWAP);
  out.volume_acc_wsol_ata = ata(out.volume_acc, prog::WSOL, prog::TOKEN);
}

void set_coin_creator(SwapKeys& k, const Pubkey& coin_creator) {
  k.coin_creator = coin_creator;
  k.coin_creator_vault_auth = pda({std::string_view("creator_vault"), coin_creator}, prog::PUMP_SWAP);
  k.coin_creator_vault_ata = ata(k.coin_creator_vault_auth, prog::WSOL, prog::TOKEN);
}

void derive_swap_keys(const UserKeys& u, const Pubkey& mint, const Pubkey& base_token_program,
                      const Pubkey& coin_creator, bool cashback, bool mayhem, SwapKeys& out) {
  out.mint = mint;
  out.base_token_program = base_token_program;
  out.pool_authority = pda({std::string_view("pool-authority"), mint}, prog::PUMP_FUN);
  const uint8_t index_le[2] = {0, 0};  // CANONICAL_POOL_INDEX
  out.pool = pda({std::string_view("pool"), Seed(index_le, 2), out.pool_authority, mint, prog::WSOL}, prog::PUMP_SWAP);
  out.pool_base_vault = ata(out.pool, mint, base_token_program);
  out.pool_quote_vault = ata(out.pool, prog::WSOL, prog::TOKEN);
  out.user_base_ata = ata(u.user, mint, base_token_program);
  out.pool_v2 = pda({std::string_view("pool-v2"), mint}, prog::PUMP_SWAP);
  out.cashback = cashback;
  out.mayhem = mayhem;
  set_coin_creator(out, coin_creator);
}

namespace {

/** Accounts 0..18, shared by buy and sell (IDL order and writability). */
void common_accounts(Ix& x, const UserKeys& u, const SwapKeys& k, const AmmConfig& cfg, const TxParams& p) {
  const Derived& d = derived();
  uint8_t fi = p.fee_recipient_index & 7;
  const Pubkey& fee_recipient = k.mayhem ? cfg.reserved_recipients[fi] : cfg.protocol_recipients[fi];
  const Pubkey& fee_ata = k.mayhem ? cfg.reserved_recipient_atas[fi] : cfg.protocol_recipient_atas[fi];
  x.rw(k.pool)
      .acc(u.user, true, true)
      .ro(d.global_config)
      .ro(k.mint)
      .ro(prog::WSOL)
      .rw(k.user_base_ata)
      .rw(u.wsol_ata)
      .rw(k.pool_base_vault)
      .rw(k.pool_quote_vault)
      .ro(fee_recipient)
      .rw(fee_ata)
      .ro(k.base_token_program)
      .ro(prog::TOKEN)
      .ro(prog::SYSTEM)
      .ro(prog::ATA)
      .ro(d.event_authority)
      .ro(prog::PUMP_SWAP)
      .rw(k.coin_creator_vault_ata)
      .ro(k.coin_creator_vault_auth);
}

void buyback_accounts(Ix& x, const SwapKeys& k, const AmmConfig& cfg, const TxParams& p) {
  if (!is_zero(k.coin_creator)) x.ro(k.pool_v2);
  uint8_t bi = p.buyback_index & 7;
  x.ro(cfg.buyback_recipients[bi]).rw(cfg.buyback_recipient_atas[bi]);
}

void budget(IxList& l, const TxParams& p) {
  if (p.cu_limit) ix::cu_limit(l, p.cu_limit);
  if (p.cu_price) ix::cu_price(l, p.cu_price);
}

void tips(IxList& l, const UserKeys& u, const TxParams& p) {
  for (uint8_t i = 0; i < p.n_tips; ++i)
    if (p.tips[i].lamports) ix::transfer(l, u.user, p.tips[i].account, p.tips[i].lamports);
}

}  // namespace

void pump_buy_ix(IxList& l, const UserKeys& u, const SwapKeys& k, const AmmConfig& cfg, const TxParams& p,
                 uint64_t base_out, uint64_t max_quote) {
  const Derived& d = derived();
  Ix& x = l.add(prog::PUMP_SWAP);
  common_accounts(x, u, k, cfg, p);
  x.ro(d.global_volume_acc).rw(u.volume_acc).ro(d.fee_config).ro(prog::PUMP_FEE);
  if (k.cashback) x.rw(u.volume_acc_wsol_ata);
  buyback_accounts(x, k, cfg, p);
  // buy(base_amount_out u64, max_quote_amount_in u64, track_volume OptionBool(true))
  x.bytes(disc::BUY, 8).u64(base_out).u64(max_quote).u8(1);
}

void pump_sell_ix(IxList& l, const UserKeys& u, const SwapKeys& k, const AmmConfig& cfg, const TxParams& p,
                  uint64_t base_in, uint64_t min_quote) {
  const Derived& d = derived();
  Ix& x = l.add(prog::PUMP_SWAP);
  common_accounts(x, u, k, cfg, p);
  x.ro(d.fee_config).ro(prog::PUMP_FEE);
  if (k.cashback) x.rw(u.volume_acc_wsol_ata).rw(u.volume_acc);
  buyback_accounts(x, k, cfg, p);
  // sell(base_amount_in u64, min_quote_amount_out u64)
  x.bytes(disc::SELL, 8).u64(base_in).u64(min_quote);
}

void buy_ixs(IxList& l, const UserKeys& u, const SwapKeys& k, const AmmConfig& cfg, const TxParams& p,
             uint64_t base_out, uint64_t max_quote) {
  l.clear();
  budget(l, p);
  ix::ata_idempotent(l, u.user, u.wsol_ata, u.user, prog::WSOL, prog::TOKEN);
  ix::transfer(l, u.user, u.wsol_ata, max_quote);
  ix::sync_native(l, u.wsol_ata);
  ix::ata_idempotent(l, u.user, k.user_base_ata, u.user, k.mint, k.base_token_program);
  pump_buy_ix(l, u, k, cfg, p, base_out, max_quote);
  ix::close_account(l, u.wsol_ata, u.user, u.user, prog::TOKEN);
  tips(l, u, p);
}

void sell_ixs(IxList& l, const UserKeys& u, const SwapKeys& k, const AmmConfig& cfg, const TxParams& p,
              uint64_t base_in, uint64_t min_quote, bool close_base) {
  l.clear();
  budget(l, p);
  ix::ata_idempotent(l, u.user, u.wsol_ata, u.user, prog::WSOL, prog::TOKEN);
  pump_sell_ix(l, u, k, cfg, p, base_in, min_quote);
  ix::close_account(l, u.wsol_ata, u.user, u.user, prog::TOKEN);
  if (close_base) ix::close_account(l, k.user_base_ata, u.user, u.user, k.base_token_program);
  tips(l, u, p);
}

}  // namespace pd
