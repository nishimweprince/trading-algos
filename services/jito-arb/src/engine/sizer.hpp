#pragma once
// Prefilter (spec §6.3) and optimal sizing (§6.4). Integer math only: the spot edge is a Q64.64
// product, the closed-form 2-hop seed uses integer square roots, and every candidate size is
// priced by the exact quoters. No floating point reaches the size that is sent.
#include <array>
#include <cstdint>
#include <memory>

#include "dex/quoter.hpp"
#include "engine/route_graph.hpp"

namespace arb::engine {

using Snapshots = std::array<std::shared_ptr<const PoolState>, MAX_LEGS>;

/** Quoter per DexKind; null = that DEX is disabled (parity not proven, or switched off). */
struct Quoters {
  const dex::IQuoter* by_kind[8]{};
  const dex::IQuoter* get(DexKind k) const { return uint8_t(k) < std::size(by_kind) ? by_kind[uint8_t(k)] : nullptr; }
};

/** Product of per-leg spot rates after fee, Q64.64 (1.0 = 2^64). */
u128 spot_product_x64(const Cycle& c, const Snapshots& s);
/** Q64.64 threshold for `min_spot_edge` given in parts per million: 2^64 * (1 + ppm / 1e6). */
u128 edge_threshold_x64(uint32_t min_edge_ppm);

struct ChainQuote {
  /** Why a chain did not quote cleanly (diagnostics; `ok` is the decision). */
  enum Fail : uint8_t { none = 0, no_quoter, quote_error, boundary_fail, partial_fill, zero_out };
  bool ok = false;        // every leg quoted, none crossed a boundary
  bool boundary = false;  // some leg needs a tick array outside the cache
  bool cache_gap = false; // ... one the bot could subscribe to (not the program's 3-array window)
  Fail fail = none;
  int8_t fail_leg = -1;
  Errc err = Errc::bad_data;  // the quoter's error when fail == quote_error
  uint64_t outs[MAX_LEGS]{};
  uint64_t final_out = 0;
  uint32_t cu = 0;
};

/** Run x through every leg with the exact quoters (later legs take the previous leg's output). */
ChainQuote quote_chain(const Cycle& c, const Snapshots& s, const Quoters& q, uint64_t x);

/** Flash fee as a function of size; 0 in own-capital mode. Returns false when the loan is impossible. */
struct FlashFeeFn {
  uint64_t fee_sf = 0;  // Kamino flash_loan_fee_sf
  bool flash = false;
  bool operator()(uint64_t x, uint64_t& fee) const;
};

struct SizerConfig {
  uint64_t min_trade = 1;
  uint64_t x_max = 0;  // already the min of reserve liquidity, depth fraction and max_trade (in base units)
  int max_evals = 24;
  uint32_t tol_ppm = 1000;  // stop refining once the bracket is within 0.1% of the size
  bool use_closed_form = true;  // seed 2-hop CP routes with the closed form (tests turn it off)
};

struct SizeResult {
  bool ok = false;
  uint64_t x = 0;
  int64_t profit = 0;  // final_out - x - flash_fee, base units
  uint64_t flash_fee = 0;
  ChainQuote chain;
  int evals = 0;
  bool retried = false;  // the 70% boundary retry ran
  // Diagnostics for a failed search: the best clean quote seen (profit may be <= 0) and the failure tally.
  bool any_clean = false;
  int64_t best_profit = INT64_MIN;
  uint64_t best_x = 0;
  int n_boundary_cache = 0;
  int n_quote_error = 0, n_boundary = 0, n_partial = 0, n_zero_out = 0, n_no_quoter = 0, n_fee = 0;
  Errc last_err = Errc::bad_data;
  int8_t last_fail_leg = -1;
};

/**
 * Closed-form optimum of a 2-hop constant-product cycle (both legs Raydium v4), from effective
 * reserves and fees: x* = (sqrt(K*A) - A) / B with K = g1 g2 ro1 ro2, A = ri1 ri2,
 * B = g1 (ri2 + g2 ro1). 0 when there is no profitable size or the route is not 2-hop CP.
 */
uint64_t closed_form_2hop_cp(const Cycle& c, const Snapshots& s);

/** Integer golden-section search for max profit (seeded by the closed form when available). */
SizeResult size_cycle(const Cycle& c, const Snapshots& s, const Quoters& q, const FlashFeeFn& fee, const SizerConfig& cfg);

/** Largest input (base units) that keeps every leg within `fraction_ppm` of its input-side depth. */
uint64_t depth_bound(const Cycle& c, const Snapshots& s, uint32_t fraction_ppm);

u128 isqrt_u128(u128 v);

}  // namespace arb::engine
