#include "tx/builders.hpp"

#include <cstring>

#include "core/crypto.hpp"
#include "core/programs.hpp"
#include "tx/dex_ix.hpp"

namespace arb::tx {

void set_compute_unit_limit(IxList& l, uint32_t units) { l.add(prog::COMPUTE_BUDGET).u8(2).u32(units); }

void set_compute_unit_price(IxList& l, uint64_t micro_lamports) { l.add(prog::COMPUTE_BUDGET).u8(3).u64(micro_lamports); }

void system_transfer(IxList& l, const Pubkey& from, const Pubkey& to, uint64_t lamports) {
  l.add(prog::SYSTEM).acc(from, true, true).rw(to).u32(2).u64(lamports);
}

size_t encode_execute_route(const ExecuteRouteArgs& a, uint8_t out[EXECUTE_ROUTE_DATA_LEN]) {
  size_t o = 0;
  out[o++] = EXECUTE_ROUTE_TAG;
  std::memcpy(out + o, &a.amount_in, 8);
  o += 8;
  std::memcpy(out + o, &a.min_out, 8);
  o += 8;
  out[o++] = a.num_legs;
  for (const LegSpec& l : a.legs) {  // all three slots are always written; only num_legs are read
    out[o++] = l.dex;
    out[o++] = l.a_to_b;
    out[o++] = l.acct_start;
    out[o++] = l.acct_len;
  }
  return o;
}

Pubkey wallet_ata(const Pubkey& wallet, const Pubkey& mint, const PoolState& p) {
  const auto& info = mint == p.mint_a ? p.mint_a_info : p.mint_b_info;
  const Pubkey& tp = info && info->token_program == prog::TOKEN_2022 ? prog::TOKEN_2022 : prog::TOKEN;
  return ata(wallet, mint, tp);
}

Result<ExecuteRouteArgs> execute_route_ix(IxList& l, const Pubkey& executor, const Pubkey& wallet, const engine::Cycle& c,
                                          std::span<const std::shared_ptr<const PoolState>> legs, uint64_t amount_in,
                                          uint64_t min_out) {
  if (c.n < 2 || c.n > 3 || legs.size() < c.n) return fail(Errc::unsupported, "route must have 2 or 3 legs");
  ExecuteRouteArgs a;
  a.amount_in = amount_in;
  a.min_out = min_out;
  a.num_legs = c.n;
  Ix& ix = l.add(executor);
  ix.acc(wallet, true, false);
  ix.rw(wallet_ata(wallet, c.base, *legs[0]));
  for (int i = 0; i + 1 < c.n; ++i) ix.rw(wallet_ata(wallet, c.legs[i].mint_out, *legs[size_t(i)]));
  uint8_t start = 0;
  for (int i = 0; i < c.n; ++i) {
    const PoolState& p = *legs[size_t(i)];
    const engine::Leg& leg = c.legs[i];
    dexix::UserAccounts ua;
    ua.authority = wallet;
    ua.token_a = wallet_ata(wallet, p.mint_a, p);
    ua.token_b = wallet_ata(wallet, p.mint_b, p);
    auto metas = dexix::swap_accounts(p, leg.a_to_b, ua);
    if (ix.n_accounts + 1 + metas.size() > MAX_IX_ACCOUNTS) return fail(Errc::too_many_keys, "executor account list");
    ix.ro(dexix::program_of(p.kind));
    for (auto& m : metas) ix.acc(m.key, m.signer, m.writable);
    a.legs[i] = LegSpec{uint8_t(p.kind), uint8_t(leg.a_to_b ? 1 : 0), start, uint8_t(1 + metas.size())};
    start = uint8_t(start + 1 + metas.size());
  }
  uint8_t data[EXECUTE_ROUTE_DATA_LEN];
  ix.bytes(data, encode_execute_route(a, data));
  return a;
}

Result<RouteTxLayout> build_route_ixs(IxList& l, const RouteTxParams& p, const engine::Cycle& c,
                                      std::span<const std::shared_ptr<const PoolState>> legs) {
  RouteTxLayout lay;
  l.clear();
  set_compute_unit_limit(l, p.cu_limit);
  if (p.cu_price_micro_lamports) set_compute_unit_price(l, p.cu_price_micro_lamports);
  Pubkey base_ata = wallet_ata(p.wallet, c.base, *legs[0]);
  if (p.reserve) {
    if (!p.reserve->enabled) return fail(Errc::disabled, "flash loans disabled on reserve");
    if (p.reserve->liquidity_mint != c.base) return fail(Errc::bad_data, "reserve mint is not the route's base");
    lay.borrow_index = l.n;
    flash::borrow_ix(l, *p.reserve, p.wallet, base_ata, p.amount_in);
  }
  lay.execute_index = l.n;
  auto args = execute_route_ix(l, p.executor, p.wallet, c, legs, p.amount_in, p.min_out);
  if (!args) return std::unexpected(args.error());
  if (p.reserve) {
    lay.repay_index = l.n;
    flash::repay_ix(l, *p.reserve, p.wallet, base_ata, p.amount_in, lay.borrow_index);
  }
  lay.tip_index = l.n;
  system_transfer(l, p.wallet, p.tip_account, p.tip_lamports);
  return lay;
}

Pubkey lookup_table_address(const Pubkey& authority, uint64_t recent_slot, uint8_t* bump) {
  uint8_t le[8];
  std::memcpy(le, &recent_slot, 8);
  Pubkey out;
  find_program_address({authority, Seed(le, 8)}, prog::ALT, out, bump);
  return out;
}

void create_lookup_table(IxList& l, const Pubkey& authority, const Pubkey& payer, uint64_t recent_slot, Pubkey* table_out) {
  uint8_t bump = 0;
  Pubkey table = lookup_table_address(authority, recent_slot, &bump);
  // web3.js AddressLookupTableProgram.createLookupTable: table (w), authority (signer), payer (signer, w), system.
  l.add(prog::ALT).rw(table).acc(authority, true, false).acc(payer, true, true).ro(prog::SYSTEM).u32(0).u64(recent_slot).u8(bump);
  if (table_out) *table_out = table;
}

void extend_lookup_table(IxList& l, const Pubkey& table, const Pubkey& authority, const Pubkey& payer,
                         std::span<const Pubkey> addresses) {
  Ix& ix = l.add(prog::ALT).rw(table).acc(authority, true, false).acc(payer, true, true).ro(prog::SYSTEM);
  ix.u32(2).u64(uint64_t(addresses.size()));
  for (const Pubkey& a : addresses) ix.bytes(a.data(), 32);
}

Result<AltAccount> decode_lookup_table(const uint8_t* d, size_t n, const Pubkey& owner) {
  if (owner != prog::ALT) return fail(Errc::bad_owner, "not a lookup table");
  constexpr size_t META = 56;
  if (n < META || (n - META) % 32 != 0) return fail(Errc::bad_length, "lookup table length");
  uint32_t type;
  std::memcpy(&type, d, 4);
  if (type != 1) return fail(Errc::bad_discriminator, "uninitialized lookup table");
  AltAccount a;
  std::memcpy(&a.deactivation_slot, d + 4, 8);
  std::memcpy(&a.last_extended_slot, d + 12, 8);
  a.last_extended_start = d[20];
  a.has_authority = d[21] == 1;
  if (a.has_authority) std::memcpy(a.authority.data(), d + 22, 32);
  for (size_t o = META; o < n; o += 32) a.addresses.push_back(key_from(d + o));
  return a;
}

}  // namespace arb::tx
