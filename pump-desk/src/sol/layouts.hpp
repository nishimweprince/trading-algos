#pragma once
// Account decoders. Offsets from the pump-amm IDL in @pump-fun/pump-swap-sdk
// 1.20.0 and pump-fun/src/enrichment/{curve,pool}.ts (verified live there).
#include <cstdint>
#include <cstring>

#include "core/bytes.hpp"
#include "core/types.hpp"
#include "sol/constants.hpp"

namespace pd {

// --- pump.fun BondingCurve -------------------------------------------------
//   8 virtual_token u64 | 16 virtual_sol u64 | 24 real_token u64 | 32 real_sol u64
//   40 total_supply u64 | 48 complete bool | 49 creator Pubkey
//   81 is_mayhem_mode bool | 82 is_cashback_coin bool
struct Curve {
  uint64_t virtual_token = 0, virtual_sol = 0, real_token = 0, real_sol = 0, supply = 0;
  bool complete = false;
  bool has_creator = false;
  Pubkey creator{};
  bool mayhem = false, cashback = false;
};

/** realTokenReserves bytes 29..31 are its top three: the band filter matches them. */
constexpr size_t CURVE_REAL_TOKEN_TOP = 29;

inline bool decode_curve(const uint8_t* d, size_t n, Curve& c) {
  if (n < 49 || std::memcmp(d, disc::BONDING_CURVE, 8) != 0) return false;
  c.virtual_token = rd_u64(d + 8);
  c.virtual_sol = rd_u64(d + 16);
  c.real_token = rd_u64(d + 24);
  c.real_sol = rd_u64(d + 32);
  c.supply = rd_u64(d + 40);
  c.complete = d[48] == 1;
  c.has_creator = n >= 81;
  if (c.has_creator) std::memcpy(c.creator.data(), d + 49, 32);
  c.mayhem = n >= 82 && d[81] == 1;
  c.cashback = n >= 83 && d[82] == 1;
  return true;
}

/**
 * Lamports to buy every remaining real token off the curve, grossed up by the
 * curve fee (src/racer/economics.ts completionCostSol). -1 = unpriced.
 */
inline double completion_cost_sol(const Curve& c, int fee_bps) {
  if (c.complete || c.real_token == 0) return 0.0;
  if (c.virtual_token == 0 || c.virtual_sol == 0 || c.virtual_token <= c.real_token) return -1.0;
  double k = double(c.virtual_token) * double(c.virtual_sol);
  double remaining = double(c.virtual_token - c.real_token);
  double cost = k / remaining - double(c.virtual_sol);
  if (!(cost >= 0)) return -1.0;
  double fee = (fee_bps < 0 ? 0 : (fee_bps > 9999 ? 9999 : fee_bps)) / 10000.0;
  return cost / (1.0 - fee) / double(LAMPORTS_PER_SOL);
}

// --- PumpSwap Pool -----------------------------------------------------------
struct Pool {
  uint8_t bump = 0;
  uint16_t index = 0;
  Pubkey creator{}, base_mint{}, quote_mint{}, lp_mint{}, base_vault{}, quote_vault{};
  uint64_t lp_supply = 0;
  Pubkey coin_creator{};
  bool mayhem = false, cashback = false;
  uint64_t virtual_quote = 0;  // i128 on chain, lamports; clamped to u64
  uint64_t creator_fee_bps = 0;
  uint32_t data_len = 0;
};

constexpr uint32_t POOL_ACCOUNT_NEW_SIZE = 300;  // smaller pools need extend_account first (SDK)

inline bool decode_pool(const uint8_t* d, size_t n, Pool& p) {
  if (n < 245 || std::memcmp(d, disc::POOL, 8) != 0) return false;
  p.bump = d[8];
  p.index = rd_u16(d + 9);
  std::memcpy(p.creator.data(), d + 11, 32);
  std::memcpy(p.base_mint.data(), d + 43, 32);
  std::memcpy(p.quote_mint.data(), d + 75, 32);
  std::memcpy(p.lp_mint.data(), d + 107, 32);
  std::memcpy(p.base_vault.data(), d + 139, 32);
  std::memcpy(p.quote_vault.data(), d + 171, 32);
  p.lp_supply = rd_u64(d + 203);
  std::memcpy(p.coin_creator.data(), d + 211, 32);
  p.mayhem = d[243] == 1;
  p.cashback = d[244] == 1;
  p.virtual_quote = 0;
  if (n >= 261) {
    i128 v = rd_i128(d + 245);
    p.virtual_quote = v <= 0 ? 0 : (v > i128(UINT64_MAX) ? UINT64_MAX : uint64_t(v));
  }
  p.creator_fee_bps = n >= 269 ? rd_u64(d + 261) : 0;
  p.data_len = static_cast<uint32_t>(n);
  return true;
}

// --- SPL token account / mint -------------------------------------------------
/** Token account: mint @0, owner @32, amount @64 (same for Token-2022). */
inline bool decode_token_amount(const uint8_t* d, size_t n, uint64_t& amount) {
  if (n < 72) return false;
  amount = rd_u64(d + 64);
  return true;
}

enum class TransferFee : uint8_t { Unknown = 0, None = 1, Present = 2 };

struct MintInfo {
  uint64_t supply = 0;
  uint8_t decimals = 0;
  bool token_2022 = false;
  TransferFee fee = TransferFee::Unknown;
};

/**
 * Mint: supply @36 u64, decimals @44. Token-2022 extensions are TLV after the
 * account-type byte at 165: u16 type, u16 length. TransferFeeConfig = type 1;
 * a non-zero fee in either epoch's schedule, or a config authority that could
 * set one, vetoes the mint.
 */
inline bool decode_mint(const uint8_t* d, size_t n, bool token_2022, MintInfo& m) {
  if (n < 82) return false;
  m.supply = rd_u64(d + 36);
  m.decimals = d[44];
  m.token_2022 = token_2022;
  if (!token_2022) { m.fee = TransferFee::None; return true; }
  m.fee = TransferFee::None;
  if (n <= 166) return true;  // bare T22 mint, no extensions
  size_t o = 166;
  while (o + 4 <= n) {
    uint16_t type = rd_u16(d + o), len = rd_u16(d + o + 2);
    if (type == 0) break;  // uninitialized padding
    if (o + 4 + len > n) { m.fee = TransferFee::Unknown; return true; }
    if (type == 1) {
      // authority 32 | withdraw authority 32 | withheld u64 | older {epoch u64, max u64, bps u16} | newer {...}
      const uint8_t* e = d + o + 4;
      if (len < 108) { m.fee = TransferFee::Unknown; return true; }
      uint64_t older_max = rd_u64(e + 80), newer_max = rd_u64(e + 98);
      uint16_t older_bps = rd_u16(e + 88), newer_bps = rd_u16(e + 106);
      // A live config authority can raise the fee next epoch: that counts as a fee.
      bool authority = !is_zero(key_from(e));
      m.fee = (authority || older_bps || newer_bps || older_max || newer_max) ? TransferFee::Present : TransferFee::None;
    }
    o += 4 + len;
  }
  return true;
}

}  // namespace pd
