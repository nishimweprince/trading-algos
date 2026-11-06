#include "state/accounts.hpp"

#include "core/crypto.hpp"

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

// CLMM PoolState (repr(C, packed), after the discriminator): bump 8, amm_config 9, owner 41, token_mint_0 73,
// token_mint_1 105, token_vault_0 137, token_vault_1 169, observation_key 201, mint decimals 233/234,
// tick_spacing 235, liquidity 237, sqrt_price_x64 253, tick_current 269, padding 273, fee_growth 277/293,
// protocol_fees 309/317, padding5 325, status 389, fee_on 390, seed_index 391, padding 393, reward_infos 397
// (3 x 169), tick_array_bitmap 904 (16 x u64), padding6 1032, fund_fees 1064/1072, open_time 1080,
// recent_epoch 1088, dynamic_fee_info 1096 (80), padding1 1176, padding2 1288..1544.
Result<ClmmPool> decode_clmm_pool(const uint8_t* d, size_t n, const Pubkey& owner) {
  if (owner != prog::RAYDIUM_CLMM) return fail(Errc::bad_owner, "clmm owner");
  if (n != CLMM_POOL_LEN) return fail(Errc::bad_length, "clmm pool length");
  if (std::memcmp(d, DISC_CLMM_POOL, 8) != 0) return fail(Errc::bad_discriminator, "clmm pool discriminator");
  ClmmPool p;
  p.amm_config = pk(d + 9);
  p.token_mint_0 = pk(d + 73);
  p.token_mint_1 = pk(d + 105);
  p.token_vault_0 = pk(d + 137);
  p.token_vault_1 = pk(d + 169);
  p.observation_key = pk(d + 201);
  p.tick_spacing = rd_u16(d + 235);
  p.liquidity = rd_u128(d + 237);
  p.sqrt_price_x64 = rd_u128(d + 253);
  p.tick_current = rd_i32(d + 269);
  p.status = d[389];
  p.fee_on = d[390];
  for (int i = 0; i < 16; ++i) p.tick_array_bitmap[i] = rd_u64(d + 904 + 8 * i);
  p.open_time = rd_u64(d + 1080);
  for (size_t i = 1096; i < 1176 && !p.dynamic_fee; ++i) p.dynamic_fee = d[i] != 0;
  if (p.tick_spacing == 0) return fail(Errc::bad_data, "clmm tick spacing 0");
  if (p.tick_current < -443636 || p.tick_current > 443636 || p.sqrt_price_x64 < u128(4295048016ULL) ||
      p.sqrt_price_x64 >= ((u128(0xfffec4b1ULL) << 64) | u128(0x845c1aa94e69579bULL)))
    return fail(Errc::bad_data, "clmm price bounds");
  if (p.fee_on > 2) return fail(Errc::bad_data, "clmm fee_on");
  return p;
}

// CLMM AmmConfig (borsh): bump 8, index 9, owner 11, protocol_fee_rate 43, trade_fee_rate 47, tick_spacing 51,
// fund_fee_rate 53, padding_u32 57, fund_owner 61, padding 93..117.
Result<ClmmConfig> decode_clmm_config(const uint8_t* d, size_t n, const Pubkey& owner) {
  if (owner != prog::RAYDIUM_CLMM) return fail(Errc::bad_owner, "clmm owner");
  if (n != CLMM_CONFIG_LEN) return fail(Errc::bad_length, "clmm config length");
  if (std::memcmp(d, DISC_CLMM_CONFIG, 8) != 0) return fail(Errc::bad_discriminator, "clmm config discriminator");
  ClmmConfig c;
  c.protocol_fee_rate = rd_u32(d + 43);
  c.trade_fee_rate = rd_u32(d + 47);
  c.tick_spacing = rd_u16(d + 51);
  c.fund_fee_rate = rd_u32(d + 53);
  if (c.trade_fee_rate >= 1'000'000) return fail(Errc::bad_data, "clmm fee rate");
  return c;
}

// CLMM TickArrayState: pool_id 8, start_tick_index 40, ticks 44 (60 x 168: tick 0, liquidity_net 4,
// liquidity_gross 20, fee growth 36/52, rewards 68, order_phase 116, orders_amount 124,
// part_filled_orders_remaining 132, unfilled_ratio 140, padding 156), initialized_tick_count 10124.
Result<ClmmTickArray> decode_clmm_tick_array(const uint8_t* d, size_t n, const Pubkey& owner) {
  if (owner != prog::RAYDIUM_CLMM) return fail(Errc::bad_owner, "clmm owner");
  if (n != CLMM_TICK_ARRAY_LEN) return fail(Errc::bad_length, "clmm tick array length");
  if (std::memcmp(d, DISC_CLMM_TICK_ARRAY, 8) != 0) return fail(Errc::bad_discriminator, "clmm tick array discriminator");
  ClmmTickArray a;
  a.pool = pk(d + 8);
  a.start_tick_index = rd_i32(d + 40);
  for (int i = 0; i < CLMM_TICK_ARRAY_SIZE; ++i) {
    const uint8_t* t = d + 44 + 168 * size_t(i);
    ClmmTick& k = a.ticks[size_t(i)];
    k.tick = rd_i32(t);
    k.liquidity_net = rd_i128(t + 4);
    k.liquidity_gross = rd_u128(t + 20);
    k.limit_orders = rd_u64(t + 124) != 0 || rd_u64(t + 132) != 0;
  }
  return a;
}

Result<ClmmBitmapExtension> decode_clmm_bitmap_extension(const uint8_t* d, size_t n, const Pubkey& owner) {
  if (owner != prog::RAYDIUM_CLMM) return fail(Errc::bad_owner, "clmm bitmap owner");
  if (n != CLMM_BITMAP_EXTENSION_LEN) return fail(Errc::bad_length, "clmm bitmap length");
  if (std::memcmp(d, DISC_CLMM_BITMAP_EXTENSION, 8)) return fail(Errc::bad_discriminator, "clmm bitmap discriminator");
  ClmmBitmapExtension e;
  std::memcpy(e.pool.data(), d + 8, 32);
  for (size_t i = 0; i < 14; ++i)
    for (size_t j = 0; j < 8; ++j) {
      e.positive[i][j] = rd_u64(d + 40 + (i * 8 + j) * 8);
      e.negative[i][j] = rd_u64(d + 936 + (i * 8 + j) * 8);
    }
  return e;
}

Pubkey clmm_bitmap_extension_pda(const Pubkey& pool) {
  return pda({std::string_view("pool_tick_array_bitmap_extension"), pool}, prog::RAYDIUM_CLMM);
}

Pubkey clmm_tick_array_pda(const Pubkey& pool, int32_t start) {
  uint8_t be[4] = {uint8_t(uint32_t(start) >> 24), uint8_t(uint32_t(start) >> 16), uint8_t(uint32_t(start) >> 8), uint8_t(uint32_t(start))};
  return pda({std::string_view("tick_array"), pool, Seed(be, 4)}, prog::RAYDIUM_CLMM);
}

// DLMM LbPair (repr C): parameters 8 (base_factor 8, filter_period 10, decay_period 12, reduction_factor 14,
// variable_fee_control 16, max_volatility_accumulator 20, min_bin_id 24, max_bin_id 28, protocol_share 32,
// base_fee_power_factor 34, function_type 35, collect_fee_mode 36), v_parameters 40 (volatility_accumulator 40,
// volatility_reference 44, index_reference 48, last_update_timestamp 56), pair_type 75, active_id 76, bin_step 80,
// status 82, activation_type 86, token_x_mint 88, token_y_mint 120, reserve_x 152, reserve_y 184,
// reward_infos 264 (2 x 144, mint first), oracle 552, bin_array_bitmap 584 (16 x u64), activation_point 816,
// token_mint_x/y_program_flag 880/881 (904 bytes).
Result<DlmmPair> decode_dlmm_pair(const uint8_t* d, size_t n, const Pubkey& owner) {
  if (owner != prog::METEORA_DLMM) return fail(Errc::bad_owner, "dlmm owner");
  if (n != DLMM_PAIR_LEN) return fail(Errc::bad_length, "dlmm pair length");
  if (std::memcmp(d, DISC_DLMM_PAIR, 8) != 0) return fail(Errc::bad_discriminator, "dlmm pair discriminator");
  DlmmPair p;
  p.base_factor = rd_u16(d + 8);
  p.filter_period = rd_u16(d + 10);
  p.decay_period = rd_u16(d + 12);
  p.reduction_factor = rd_u16(d + 14);
  p.variable_fee_control = rd_u32(d + 16);
  p.max_volatility_accumulator = rd_u32(d + 20);
  p.min_bin_id = rd_i32(d + 24);
  p.max_bin_id = rd_i32(d + 28);
  p.protocol_share = rd_u16(d + 32);
  p.base_fee_power_factor = d[34];
  p.function_type = d[35];
  p.collect_fee_mode = d[36];
  p.volatility_accumulator = rd_u32(d + 40);
  p.volatility_reference = rd_u32(d + 44);
  p.index_reference = rd_i32(d + 48);
  p.last_update_timestamp = int64_t(rd_u64(d + 56));
  p.pair_type = d[75];
  p.active_id = rd_i32(d + 76);
  p.bin_step = rd_u16(d + 80);
  p.status = d[82];
  p.activation_type = d[86];
  p.token_x_mint = pk(d + 88);
  p.token_y_mint = pk(d + 120);
  p.reserve_x = pk(d + 152);
  p.reserve_y = pk(d + 184);
  p.has_rewards = !is_zero(pk(d + 264)) || !is_zero(pk(d + 264 + 144));
  p.oracle = pk(d + 552);
  for (int i = 0; i < 16; ++i) p.bin_array_bitmap[i] = rd_u64(d + 584 + 8 * i);
  p.activation_point = rd_u64(d + 816);
  p.token_x_program_flag = d[880];
  p.token_y_program_flag = d[881];
  if (p.bin_step == 0) return fail(Errc::bad_data, "dlmm bin step 0");
  return p;
}

// DLMM BinArray: index 8 (i64), version 16, lb_pair 24, bins 56 (70 x 144: amount_x 0, amount_y 8, price 16,
// liquidity_supply 32, ..., open_order_amount 112, total_processing_order_amount 120,
// processed_order_remaining_amount 128, order_age 136, limit_order_ask_side 140).
Result<DlmmBinArray> decode_dlmm_bin_array(const uint8_t* d, size_t n, const Pubkey& owner) {
  if (owner != prog::METEORA_DLMM) return fail(Errc::bad_owner, "dlmm owner");
  if (n != DLMM_BIN_ARRAY_LEN) return fail(Errc::bad_length, "dlmm bin array length");
  if (std::memcmp(d, DISC_DLMM_BIN_ARRAY, 8) != 0) return fail(Errc::bad_discriminator, "dlmm bin array discriminator");
  DlmmBinArray a;
  a.index = int64_t(rd_u64(d + 8));
  a.pair = pk(d + 24);
  for (int i = 0; i < DLMM_BINS_PER_ARRAY; ++i) {
    const uint8_t* b = d + 56 + 144 * size_t(i);
    DlmmBin& x = a.bins[size_t(i)];
    x.amount_x = rd_u64(b);
    x.amount_y = rd_u64(b + 8);
    x.price = rd_u128(b + 16);
    x.open_order_amount = rd_u64(b + 112);
    x.processed_order_remaining = rd_u64(b + 128);
    x.limit_order_ask_side = b[140] != 0;
  }
  return a;
}

Pubkey dlmm_bin_array_pda(const Pubkey& pair, int64_t index) {
  uint8_t le[8];
  std::memcpy(le, &index, 8);
  return pda({std::string_view("bin_array"), pair, Seed(le, 8)}, prog::METEORA_DLMM);
}

// PumpSwap Pool (borsh, after the discriminator): pool_bump 8, index 9, creator 11, base_mint 43, quote_mint 75,
// lp_mint 107, pool_base_token_account 139, pool_quote_token_account 171, lp_supply 203, coin_creator 211,
// is_mayhem_mode 243, is_cashback_coin 244, virtual_quote_reserves 245 (i128), creator_fee_bps 261,
// can_edit_creator_fee 269, is_holder_reward 270, protocol_fees 271, creator_fees 279 (287 bytes in full).
Result<PumpPool> decode_pump_pool(const uint8_t* d, size_t n, const Pubkey& owner) {
  if (owner != prog::PUMPSWAP) return fail(Errc::bad_owner, "pumpswap owner");
  if (n < PUMP_POOL_MIN_LEN) return fail(Errc::bad_length, "pumpswap pool length");
  if (std::memcmp(d, DISC_PUMP_POOL, 8) != 0) return fail(Errc::bad_discriminator, "pumpswap pool discriminator");
  PumpPool p;
  p.index = rd_u16(d + 9);
  p.creator = pk(d + 11);
  p.base_mint = pk(d + 43);
  p.quote_mint = pk(d + 75);
  p.pool_base_token_account = pk(d + 139);
  p.pool_quote_token_account = pk(d + 171);
  if (n >= 243) p.coin_creator = pk(d + 211);
  if (n >= 244) p.is_mayhem_mode = d[243] != 0;
  if (n >= 245) p.is_cashback_coin = d[244] != 0;
  if (n >= 261) p.virtual_quote_reserves = i128(rd_u128(d + 245));
  if (n >= 269) p.creator_fee_bps = rd_u64(d + 261);
  if (n >= 279) p.protocol_fees = rd_u64(d + 271);
  if (n >= 287) p.creator_fees = rd_u64(d + 279);
  return p;
}

// PumpSwap GlobalConfig: admin 8, lp_fee_basis_points 40, protocol_fee_basis_points 48, disable_flags 56,
// protocol_fee_recipients 57 (8x32), coin_creator_fee_basis_points 313, admin_set_coin_creator_authority 321,
// whitelist_pda 353, reserved_fee_recipient 385, mayhem_mode_enabled 417, reserved_fee_recipients 418 (7x32),
// is_cashback_enabled 642, buyback_fee_recipients 643 (8x32), buyback_basis_points 899, boost_authority 907,
// boost_enabled 939, creator_fee_configurable 940, max_configurable_creator_fee_bps 941 (949 bytes).
Result<PumpGlobalConfig> decode_pump_global_config(const uint8_t* d, size_t n, const Pubkey& owner) {
  if (owner != prog::PUMPSWAP) return fail(Errc::bad_owner, "pumpswap owner");
  if (n < 899) return fail(Errc::bad_length, "pumpswap global config length");
  if (std::memcmp(d, DISC_PUMP_GLOBAL_CONFIG, 8) != 0) return fail(Errc::bad_discriminator, "pumpswap global config discriminator");
  PumpGlobalConfig g;
  g.disable_flags = d[56];
  for (int i = 0; i < 8; ++i) g.buyback_fee_recipients[i] = pk(d + 643 + 32 * i);
  if (n > 940) g.creator_fee_configurable = d[940] != 0;
  return g;
}

// Pump Fees FeeConfig (borsh): bump 8, admin 9, flat_fees 41 (3 x u64), fee_tiers (u32 len + len x (u128, 3 x u64)),
// stable_fee_tiers (same), exotic_flat_fees (3 x u64; zeros when the account predates it).
Result<PumpFeeConfig> decode_pump_fee_config(const uint8_t* d, size_t n, const Pubkey& owner) {
  if (owner != prog::PUMP_FEES) return fail(Errc::bad_owner, "pump fees owner");
  if (n < 65 + 8) return fail(Errc::bad_length, "pump fee config length");
  if (std::memcmp(d, DISC_PUMP_FEE_CONFIG, 8) != 0) return fail(Errc::bad_discriminator, "pump fee config discriminator");
  PumpFeeConfig c;
  size_t o = 41;
  auto fees = [&](PumpFees& f) {
    f.lp_fee_bps = rd_u64(d + o);
    f.protocol_fee_bps = rd_u64(d + o + 8);
    f.creator_fee_bps = rd_u64(d + o + 16);
    o += 24;
  };
  fees(c.flat_fees);
  for (auto* tiers : {&c.fee_tiers, &c.stable_fee_tiers}) {
    if (o + 4 > n) return fail(Errc::bad_length, "pump fee config tiers");
    uint32_t len = rd_u32(d + o);
    o += 4;
    if (len > 64 || o + size_t(len) * 40 > n) return fail(Errc::bad_data, "pump fee config tier count");
    for (uint32_t i = 0; i < len; ++i) {
      PumpFeeTier t;
      t.market_cap_lamports_threshold = rd_u128(d + o);
      o += 16;
      fees(t.fees);
      tiers->push_back(t);
    }
  }
  if (o + 24 <= n) fees(c.exotic_flat_fees);
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
