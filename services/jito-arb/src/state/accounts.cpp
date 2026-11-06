#include "state/accounts.hpp"

#include <cstring>

#include "core/programs.hpp"

namespace arb::state {

namespace {

bool is_token_program(const Pubkey& p) { return p == prog::TOKEN || p == prog::TOKEN_2022; }
Pubkey pk(const uint8_t* p) { return key_from(p); }

}  // namespace

uint64_t transfer_fee(const TransferFee& f, uint64_t amount) {
  if (f.basis_points == 0 || amount == 0) return 0;
  u128 num = u128(amount) * f.basis_points;
  u128 raw = (num + 9999) / 10000;
  if (raw > f.maximum_fee) return f.maximum_fee;
  return uint64_t(raw);
}

// spl-token Account::unpack: mint 0, owner 32, amount 64, delegate COption 72 (4+32),
// state 108, is_native COption<u64> 109 (4+8), delegated_amount 121, close_authority COption 129 (4+32).
// Token-2022 accounts carry the same 165-byte base, then account_type (2) and TLV.
Result<TokenAccount> decode_token_account(const uint8_t* d, size_t n, const Pubkey& owner) {
  if (!is_token_program(owner)) return fail(Errc::bad_owner, "token account owner");
  if (n < TOKEN_ACCOUNT_LEN) return fail(Errc::bad_length, "token account length");
  if (owner == prog::TOKEN && n != TOKEN_ACCOUNT_LEN) return fail(Errc::bad_length, "spl token account length");
  if (owner == prog::TOKEN_2022 && n > TOKEN_ACCOUNT_LEN && d[TOKEN_ACCOUNT_LEN] != 2)
    return fail(Errc::bad_discriminator, "token-2022 account type");
  TokenAccount a;
  a.mint = pk(d);
  a.owner = pk(d + 32);
  a.amount = rd_u64(d + 64);
  a.has_delegate = rd_u32(d + 72) == 1;
  a.state = d[108];
  a.is_native = rd_u32(d + 109) == 1;
  a.has_close_authority = rd_u32(d + 129) == 1;
  a.token_program = owner;
  if (a.state == 0) return fail(Errc::bad_data, "token account uninitialized");
  return a;
}

// spl-token Mint::unpack: mint_authority COption 0 (4+32), supply 36, decimals 44, is_initialized 45,
// freeze_authority COption 46 (4+32). Token-2022: zero padding to 165, account_type 1 at 165, TLV from 166:
// [type u16][length u16][value].
Result<Mint> decode_mint(const uint8_t* d, size_t n, const Pubkey& owner) {
  if (!is_token_program(owner)) return fail(Errc::bad_owner, "mint owner");
  if (n < MINT_LEN) return fail(Errc::bad_length, "mint length");
  if (owner == prog::TOKEN && n != MINT_LEN) return fail(Errc::bad_length, "spl mint length");
  Mint m;
  m.has_mint_authority = rd_u32(d) == 1;
  m.supply = rd_u64(d + 36);
  m.decimals = d[44];
  if (d[45] != 1) return fail(Errc::bad_data, "mint uninitialized");
  m.has_freeze_authority = rd_u32(d + 46) == 1;
  if (m.has_freeze_authority) m.freeze_authority = pk(d + 50);
  m.token_program = owner;
  if (owner == prog::TOKEN_2022 && n > MINT_LEN) {
    if (n < TOKEN_ACCOUNT_LEN + 1 || d[TOKEN_ACCOUNT_LEN] != 1) return fail(Errc::bad_discriminator, "token-2022 mint type");
    size_t o = TOKEN_ACCOUNT_LEN + 1;
    while (o + 4 <= n) {
      uint16_t type = rd_u16(d + o);
      uint16_t len = rd_u16(d + o + 2);
      o += 4;
      if (type == 0) break;  // Uninitialized: the rest is padding
      if (o + len > n) return fail(Errc::bad_length, "token-2022 TLV overrun");
      if (type < 64) m.ext_mask |= uint64_t(1) << type;
      if (m.n_ext < 24) m.ext_types[m.n_ext++] = type;
      const uint8_t* v = d + o;
      if (type == ext::TransferFeeConfig) {
        // TransferFeeConfig: two OptionalNonZeroPubkey authorities (64), withheld_amount (8),
        // older_transfer_fee {epoch u64, maximum_fee u64, bps u16} (18), newer_transfer_fee (18).
        if (len < 108) return fail(Errc::bad_length, "transfer fee config");
        m.has_transfer_fee = true;
        m.older_fee = {rd_u64(v + 72), rd_u64(v + 80), rd_u16(v + 88)};
        m.newer_fee = {rd_u64(v + 90), rd_u64(v + 98), rd_u16(v + 106)};
      } else if (type == ext::DefaultAccountState && len >= 1) {
        m.default_account_state = v[0];
      }
      o += len;
    }
  }
  return m;
}

// AmmInfo (repr C, all u64/u128/Pubkey, no discriminator): 16 u64 header (0..128), Fees 8 u64 (128..192),
// StateData (192..336), then the pubkeys.
Result<RaydiumV4> decode_raydium_v4(const uint8_t* d, size_t n, const Pubkey& owner) {
  if (owner != prog::RAYDIUM_V4) return fail(Errc::bad_owner, "raydium owner");
  if (n != RAYDIUM_V4_LEN) return fail(Errc::bad_length, "raydium length");
  RaydiumV4 a;
  a.status = rd_u64(d + 0);
  a.nonce = rd_u64(d + 8);
  a.coin_decimals = rd_u64(d + 32);
  a.pc_decimals = rd_u64(d + 40);
  // Fees { min_separate_num, min_separate_den, trade_fee_num, trade_fee_den, pnl_num, pnl_den, swap_fee_num, swap_fee_den }
  a.trade_fee_numerator = rd_u64(d + 128 + 16);
  a.trade_fee_denominator = rd_u64(d + 128 + 24);
  a.swap_fee_numerator = rd_u64(d + 128 + 48);
  a.swap_fee_denominator = rd_u64(d + 128 + 56);
  // StateData { need_take_pnl_coin, need_take_pnl_pc, total_pnl_pc, total_pnl_coin, pool_open_time, ... }
  a.need_take_pnl_coin = rd_u64(d + 192);
  a.need_take_pnl_pc = rd_u64(d + 200);
  a.pool_open_time = rd_u64(d + 224);
  a.coin_vault = pk(d + 336);
  a.pc_vault = pk(d + 368);
  a.coin_mint = pk(d + 400);
  a.pc_mint = pk(d + 432);
  a.lp_mint = pk(d + 464);
  a.open_orders = pk(d + 496);
  a.market = pk(d + 528);
  a.market_program = pk(d + 560);
  a.target_orders = pk(d + 592);
  a.amm_owner = pk(d + 688);
  a.lp_amount = rd_u64(d + 720);
  if (a.swap_fee_denominator == 0) return fail(Errc::bad_data, "raydium zero fee denominator");
  return a;
}

// CPMM PoolState (repr(C, packed), after the discriminator): amm_config 8, pool_creator 40, token_0_vault 72,
// token_1_vault 104, lp_mint 136, token_0_mint 168, token_1_mint 200, token_0_program 232, token_1_program 264,
// observation_key 296, auth_bump 328, status 329, lp/mint decimals 330..332, lp_supply 333, protocol_fees 341/349,
// fund_fees 357/365, open_time 373, recent_epoch 381, creator_fee_on 389, enable_creator_fee 390, padding1 391,
// creator_fees 397/405, padding 413..637.
Result<CpmmPool> decode_cpmm_pool(const uint8_t* d, size_t n, const Pubkey& owner) {
  if (owner != prog::RAYDIUM_CPMM) return fail(Errc::bad_owner, "cpmm owner");
  if (n != CPMM_POOL_LEN) return fail(Errc::bad_length, "cpmm pool length");
  if (std::memcmp(d, DISC_CPMM_POOL, 8) != 0) return fail(Errc::bad_discriminator, "cpmm pool discriminator");
  CpmmPool p;
  p.amm_config = pk(d + 8);
  p.token_0_vault = pk(d + 72);
  p.token_1_vault = pk(d + 104);
  p.token_0_mint = pk(d + 168);
  p.token_1_mint = pk(d + 200);
  p.token_0_program = pk(d + 232);
  p.token_1_program = pk(d + 264);
  p.observation_key = pk(d + 296);
  p.auth_bump = d[328];
  p.status = d[329];
  p.protocol_fees_0 = rd_u64(d + 341);
  p.protocol_fees_1 = rd_u64(d + 349);
  p.fund_fees_0 = rd_u64(d + 357);
  p.fund_fees_1 = rd_u64(d + 365);
  p.open_time = rd_u64(d + 373);
  p.creator_fee_on = d[389];
  p.enable_creator_fee = d[390] != 0;
  p.creator_fees_0 = rd_u64(d + 397);
  p.creator_fees_1 = rd_u64(d + 405);
  if (p.creator_fee_on > 2) return fail(Errc::bad_data, "cpmm creator_fee_on");
  return p;
}

// CPMM AmmConfig (borsh): bump 8, disable_create_pool 9, index 10, trade_fee_rate 12, protocol_fee_rate 20,
// fund_fee_rate 28, create_pool_fee 36, protocol_owner 44, fund_owner 76, creator_fee_rate 108,
// creator_fee_share_rate 116, padding 124..236.
Result<CpmmConfig> decode_cpmm_config(const uint8_t* d, size_t n, const Pubkey& owner) {
  if (owner != prog::RAYDIUM_CPMM) return fail(Errc::bad_owner, "cpmm owner");
  if (n != CPMM_CONFIG_LEN) return fail(Errc::bad_length, "cpmm config length");
  if (std::memcmp(d, DISC_CPMM_CONFIG, 8) != 0) return fail(Errc::bad_discriminator, "cpmm config discriminator");
  CpmmConfig c;
  c.trade_fee_rate = rd_u64(d + 12);
  c.protocol_fee_rate = rd_u64(d + 20);
  c.fund_fee_rate = rd_u64(d + 28);
  c.creator_fee_rate = rd_u64(d + 108);
  if (c.trade_fee_rate + c.creator_fee_rate >= 1'000'000) return fail(Errc::bad_data, "cpmm fee rate");
  return c;
}

// Whirlpool (after the 8-byte discriminator): whirlpools_config 8, bump 40, tick_spacing 41, fee_tier_index_seed 43,
// fee_rate 45, protocol_fee_rate 47, liquidity 49, sqrt_price 65, tick_current_index 81, protocol_fee_owed_a 85,
// protocol_fee_owed_b 93, token_mint_a 101, token_vault_a 133, fee_growth_global_a 165, token_mint_b 181,
// token_vault_b 213, fee_growth_global_b 245, reward_last_updated_timestamp 261, reward_infos 269..653.
Result<Whirlpool> decode_whirlpool(const uint8_t* d, size_t n, const Pubkey& owner) {
  if (owner != prog::WHIRLPOOL) return fail(Errc::bad_owner, "whirlpool owner");
  if (n != WHIRLPOOL_LEN) return fail(Errc::bad_length, "whirlpool length");
  if (std::memcmp(d, DISC_WHIRLPOOL, 8) != 0) return fail(Errc::bad_discriminator, "whirlpool discriminator");
  Whirlpool w;
  w.whirlpools_config = pk(d + 8);
  w.tick_spacing = rd_u16(d + 41);
  w.fee_tier_index = rd_u16(d + 43);
  w.fee_rate = rd_u16(d + 45);
  w.protocol_fee_rate = rd_u16(d + 47);
  w.liquidity = rd_u128(d + 49);
  w.sqrt_price = rd_u128(d + 65);
  w.tick_current_index = rd_i32(d + 81);
  w.protocol_fee_owed_a = rd_u64(d + 85);
  w.protocol_fee_owed_b = rd_u64(d + 93);
  w.token_mint_a = pk(d + 101);
  w.token_vault_a = pk(d + 133);
  w.fee_growth_global_a = rd_u128(d + 165);
  w.token_mint_b = pk(d + 181);
  w.token_vault_b = pk(d + 213);
  w.fee_growth_global_b = rd_u128(d + 245);
  if (w.tick_spacing == 0) return fail(Errc::bad_data, "whirlpool tick spacing 0");
  return w;
}

// FixedTickArray (repr C packed): start_tick_index i32 at 8, 88 x Tick(113) from 12, whirlpool at 9956.
//   Tick: initialized u8, liquidity_net i128, liquidity_gross u128, fee/reward growths (80).
// DynamicTickArray: start_tick_index i32 at 8, whirlpool at 12, tick_bitmap u128 at 44, then 88 ticks of
//   [tag u8: 0 uninitialized | 1 initialized + DynamicTickData(112): liquidity_net, liquidity_gross, ...].
Result<TickArray> decode_tick_array(const uint8_t* d, size_t n, const Pubkey& owner) {
  if (owner != prog::WHIRLPOOL) return fail(Errc::bad_owner, "tick array owner");
  if (n < 8) return fail(Errc::bad_length, "tick array length");
  TickArray t;
  if (std::memcmp(d, DISC_FIXED_TICK_ARRAY, 8) == 0) {
    if (n != FIXED_TICK_ARRAY_LEN) return fail(Errc::bad_length, "fixed tick array length");
    t.start_tick_index = rd_i32(d + 8);
    for (int i = 0; i < TICK_ARRAY_SIZE; ++i) {
      const uint8_t* p = d + 12 + size_t(i) * 113;
      t.ticks[size_t(i)] = Tick{p[0] != 0, rd_i128(p + 1), rd_u128(p + 17)};
    }
    t.whirlpool = pk(d + 9956);
    return t;
  }
  if (std::memcmp(d, DISC_DYNAMIC_TICK_ARRAY, 8) == 0) {
    if (n < DYNAMIC_TICK_ARRAY_MIN_LEN) return fail(Errc::bad_length, "dynamic tick array length");
    t.dynamic = true;
    t.start_tick_index = rd_i32(d + 8);
    t.whirlpool = pk(d + 12);
    u128 bitmap = rd_u128(d + 44);
    size_t o = 60;
    for (int i = 0; i < TICK_ARRAY_SIZE; ++i) {
      if (o >= n) return fail(Errc::bad_length, "dynamic tick array truncated");
      uint8_t tag = d[o++];
      if (tag == 1) {
        if (o + 112 > n) return fail(Errc::bad_length, "dynamic tick truncated");
        t.ticks[size_t(i)] = Tick{true, rd_i128(d + o), rd_u128(d + o + 16)};
        o += 112;
      } else if (tag != 0) {
        return fail(Errc::bad_data, "dynamic tick tag");
      }
      if (bool((bitmap >> i) & 1) != t.ticks[size_t(i)].initialized) return fail(Errc::bad_data, "dynamic tick bitmap");
    }
    return t;
  }
  return fail(Errc::bad_discriminator, "tick array discriminator");
}

// Reserve (repr C, after the 8-byte discriminator): version u64 8, LastUpdate{slot u64, stale u8, price_status u8,
// placeholder [u8;6]} 16, lending_market 32, farm_collateral 64, farm_debt 96, ReserveLiquidity 128 {mint_pubkey 128,
// supply_vault 160, fee_vault 192, total_available_amount 224, borrowed_amount_sf 232, market_price_sf 248,
// market_price_last_updated_ts 264, mint_decimals 272, ..., token_program 408}; reserve_liquidity_padding [u64;150];
// ReserveCollateral (1096); reserve_collateral_padding [u64;150]; ReserveConfig at 4856 {status u8 4856, ...,
// fees{origination_fee_sf 4896, flash_loan_fee_sf 4904}}.
Result<KaminoReserve> decode_kamino_reserve(const uint8_t* d, size_t n, const Pubkey& owner) {
  if (owner != prog::KLEND) return fail(Errc::bad_owner, "reserve owner");
  if (n != KAMINO_RESERVE_LEN) return fail(Errc::bad_length, "reserve length");
  if (std::memcmp(d, DISC_KAMINO_RESERVE, 8) != 0) return fail(Errc::bad_discriminator, "reserve discriminator");
  KaminoReserve r;
  r.last_update_slot = rd_u64(d + 16);
  r.lending_market = pk(d + 32);
  r.liquidity_mint = pk(d + 128);
  r.supply_vault = pk(d + 160);
  r.fee_vault = pk(d + 192);
  r.available_amount = rd_u64(d + 224);
  r.mint_decimals = rd_u64(d + 272);
  r.token_program = pk(d + 408);
  r.status = d[4856];
  r.flash_loan_fee_sf = rd_u64(d + 4904);
  return r;
}

}  // namespace arb::state
