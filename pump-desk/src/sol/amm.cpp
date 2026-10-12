#include "sol/amm.hpp"

#include <cmath>
#include <cstring>

#include "sol/constants.hpp"
#include "sol/crypto.hpp"

namespace pd {

namespace {
u128 ceil_div(u128 a, u128 b) { return (a + b - 1) / b; }
u128 fee(u128 amount, uint64_t bps) { return ceil_div(amount * bps, 10000); }

/** floor((1 ± s/100) * 1e9) with the same IEEE double ops as the SDK. */
uint64_t slippage_factor(double slippage_pct, bool up) {
  double f = (up ? (1.0 + slippage_pct / 100.0) : (1.0 - slippage_pct / 100.0)) * 1e9;
  return f <= 0 ? 0 : static_cast<uint64_t>(std::floor(f));
}
}  // namespace

bool decode_global_config(const uint8_t* d, size_t n, AmmConfig& c) {
  if (n < 940 || std::memcmp(d, disc::GLOBAL_CONFIG, 8) != 0) return false;
  c.lp_bps = rd_u64(d + 40);
  c.protocol_bps = rd_u64(d + 48);
  c.disable_flags = d[56];
  for (int i = 0; i < 8; ++i) std::memcpy(c.protocol_recipients[i].data(), d + 57 + 32 * i, 32);
  c.coin_creator_bps = rd_u64(d + 313);
  std::memcpy(c.reserved_recipients[0].data(), d + 385, 32);
  for (int i = 0; i < 7; ++i) std::memcpy(c.reserved_recipients[i + 1].data(), d + 418 + 32 * i, 32);
  for (int i = 0; i < 8; ++i) std::memcpy(c.buyback_recipients[i].data(), d + 643 + 32 * i, 32);
  c.creator_fee_configurable = n >= 941 && d[940] == 1;
  // WSOL ATAs (quote token program = SPL Token) of every recipient, off the hot path.
  for (int i = 0; i < 8; ++i) {
    c.protocol_recipient_atas[i] = ata(c.protocol_recipients[i], prog::WSOL, prog::TOKEN);
    c.reserved_recipient_atas[i] = ata(c.reserved_recipients[i], prog::WSOL, prog::TOKEN);
    c.buyback_recipient_atas[i] = ata(c.buyback_recipients[i], prog::WSOL, prog::TOKEN);
  }
  return true;
}

bool decode_fee_config(const uint8_t* d, size_t n, AmmConfig& c) {
  if (n < 69 || std::memcmp(d, disc::FEE_CONFIG, 8) != 0) return false;
  // bump u8 @8 | admin @9 | flat_fees @41 (3 x u64) | fee_tiers vec @65 | stable_fee_tiers vec | exotic
  c.flat = Fees{rd_u64(d + 41), rd_u64(d + 49), rd_u64(d + 57)};
  uint32_t count = rd_u32(d + 65);
  size_t o = 69;
  if (count > MAX_FEE_TIERS || o + size_t(count) * 40 > n) return false;
  c.n_tiers = count;
  for (uint32_t i = 0; i < count; ++i, o += 40) {
    c.tiers[i].threshold = rd_u128(d + o);
    c.tiers[i].fees = Fees{rd_u64(d + o + 16), rd_u64(d + o + 24), rd_u64(d + o + 32)};
  }
  c.has_fee_config = true;
  return true;
}

FeeBps compute_fees_bps(const AmmConfig& cfg, bool is_pump_pool, uint64_t mint_supply, uint64_t base_reserve,
                        uint64_t effective_quote, bool mayhem, uint64_t pool_creator_fee_bps) {
  FeeBps out;
  if (!cfg.has_fee_config) {
    out = FeeBps{cfg.lp_bps, cfg.protocol_bps, cfg.coin_creator_bps};
    return out;
  }
  Fees f;
  if (!is_pump_pool || cfg.n_tiers == 0) {
    f = cfg.flat;
  } else {
    // poolMarketCap: quote * circulating / base
    u128 supply = mayhem ? PUMP_AMM_TOTAL_SUPPLY : mint_supply;
    u128 mcap = base_reserve == 0 ? 0 : (u128(effective_quote) * supply) / base_reserve;
    f = cfg.tiers[0].fees;
    if (mcap >= cfg.tiers[0].threshold) {
      for (uint32_t i = cfg.n_tiers; i-- > 0;) {
        if (mcap >= cfg.tiers[i].threshold) { f = cfg.tiers[i].fees; break; }
      }
    }
  }
  out = FeeBps{f.lp, f.protocol, f.creator};
  if (cfg.creator_fee_configurable && pool_creator_fee_bps > 0) out.creator = pool_creator_fee_bps;
  return out;
}

BuyQuote buy_quote_input(uint64_t quote, double slippage_pct, uint64_t base_reserve, uint64_t quote_reserve,
                         uint64_t virtual_quote, const FeeBps& fees, bool coin_creator_is_default) {
  BuyQuote q;
  if (base_reserve == 0 || quote_reserve == 0 || quote < 2) return q;
  u128 effective_reserve = u128(quote_reserve) + virtual_quote;
  uint64_t creator_bps = coin_creator_is_default ? 0 : fees.creator;
  u128 total_fee_bps = u128(fees.lp) + fees.protocol + creator_bps;
  u128 effective_quote = (u128(quote) * 10000) / (10000 + total_fee_bps);
  u128 total = effective_quote + fee(effective_quote, fees.lp) + fee(effective_quote, fees.protocol) +
               (coin_creator_is_default ? 0 : fee(effective_quote, fees.creator));
  if (total > quote) effective_quote -= (total - quote);
  if (effective_quote < 1) return q;
  u128 input = effective_quote - 1;
  u128 denom = effective_reserve + input;
  if (denom == 0) return q;
  q.base_out = static_cast<uint64_t>((u128(base_reserve) * input) / denom);
  q.max_quote = static_cast<uint64_t>((u128(quote) * slippage_factor(slippage_pct, true)) / 1'000'000'000u);
  q.ok = q.base_out > 0;
  return q;
}

SellQuote sell_base_input(uint64_t base, double slippage_pct, uint64_t base_reserve, uint64_t quote_reserve,
                          uint64_t virtual_quote, const FeeBps& fees, bool coin_creator_is_default) {
  SellQuote q;
  if (base_reserve == 0 || quote_reserve == 0 || base == 0) return q;
  u128 effective_reserve = u128(quote_reserve) + virtual_quote;
  u128 out = (effective_reserve * base) / (u128(base_reserve) + base);
  u128 lp = fee(out, fees.lp), protocol = fee(out, fees.protocol);
  u128 creator = coin_creator_is_default ? 0 : fee(out, fees.creator);
  if (u128(quote_reserve) < out - lp) return q;  // real vault can't pay: "Insufficient real quote reserves"
  if (out < lp + protocol + creator) return q;
  u128 final_quote = out - lp - protocol - creator;
  q.final_quote = static_cast<uint64_t>(final_quote);
  q.min_quote = static_cast<uint64_t>((final_quote * slippage_factor(slippage_pct, false)) / 1'000'000'000u);
  q.ok = true;
  return q;
}

}  // namespace pd
