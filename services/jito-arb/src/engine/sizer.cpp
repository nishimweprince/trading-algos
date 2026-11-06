#include "engine/sizer.hpp"

#include <algorithm>

#include "core/u256.hpp"
#include "flash/kamino.hpp"

namespace arb::engine {

namespace {

constexpr u128 ONE_X64 = u128(1) << 64;

u128 mul_shr64_sat(u128 a, u128 b) {
  U256 p = U256::mul(a, b).shr(64);
  return p.fits_u128() ? p.lo128() : ~u128(0);
}

// Raydium v4 effective reserves (vault - need_take_pnl) and fee keep-fraction n/d, as the quoter uses them.
struct CpLeg {
  u128 ri = 0, ro = 0;
  u128 n = 0, d = 0;
};

bool cp_leg(const PoolState& p, bool a_to_b, CpLeg& out) {
  if (p.kind != DexKind::RaydiumV4 || p.ray.swap_fee_denominator == 0) return false;
  if (p.vault_a_amount < p.ray.need_take_pnl_coin || p.vault_b_amount < p.ray.need_take_pnl_pc) return false;
  u128 coin = p.vault_a_amount - p.ray.need_take_pnl_coin, pc = p.vault_b_amount - p.ray.need_take_pnl_pc;
  out.ri = a_to_b ? coin : pc;
  out.ro = a_to_b ? pc : coin;
  out.d = p.ray.swap_fee_denominator;
  if (p.ray.swap_fee_numerator > p.ray.swap_fee_denominator) return false;
  out.n = out.d - p.ray.swap_fee_numerator;
  return out.ri > 0 && out.ro > 0;
}

}  // namespace

u128 isqrt_u128(u128 v) {
  if (v < 2) return v;
  // Newton from a power-of-two upper bound; converges monotonically from above.
  uint64_t hi = uint64_t(v >> 64);
  int bits = hi ? 128 - __builtin_clzll(hi) : 64 - __builtin_clzll(uint64_t(v));
  u128 x = u128(1) << ((bits + 1) / 2);
  for (;;) {
    u128 y = (x + v / x) >> 1;
    if (y >= x) return x;
    x = y;
  }
}

u128 spot_product_x64(const Cycle& c, const Snapshots& s) {
  u128 r = ONE_X64;
  for (int i = 0; i < c.n; ++i) r = mul_shr64_sat(r, dex::spot_rate_x64(*s[i], c.legs[i].a_to_b));
  return r;
}

u128 edge_threshold_x64(uint32_t ppm) { return ONE_X64 + ONE_X64 / 1'000'000 * ppm; }

ChainQuote quote_chain(const Cycle& c, const Snapshots& s, const Quoters& q, uint64_t x) {
  ChainQuote out;
  uint64_t amt = x;
  for (int i = 0; i < c.n; ++i) {
    const dex::IQuoter* qq = q.get(s[i]->kind);
    auto failed = [&](ChainQuote::Fail f) {
      out.fail = f;
      out.fail_leg = int8_t(i);
      return out;
    };
    if (!qq) return failed(ChainQuote::no_quoter);
    auto r = qq->quote_exact_in(*s[i], c.legs[i].a_to_b, amt);
    if (!r) {
      out.err = r.error().code;
      return failed(ChainQuote::quote_error);
    }
    if (r->crosses_boundary) {
      out.boundary = true;
      out.cache_gap = r->cache_gap;
      return failed(ChainQuote::boundary_fail);
    }
    // A leg that stops short of its input (Whirlpool price limit) leaves tokens mid-route: not a clean cycle.
    if (r->amount_in != amt) return failed(ChainQuote::partial_fill);
    if (r->amount_out == 0) return failed(ChainQuote::zero_out);
    out.outs[i] = r->amount_out;
    out.cu += r->est_compute_units;
    amt = r->amount_out;
  }
  out.final_out = amt;
  out.ok = true;
  return out;
}

bool FlashFeeFn::operator()(uint64_t x, uint64_t& fee) const {
  fee = 0;
  if (!flash) return true;
  auto r = flash::flash_loan_fee(x, fee_sf);
  if (!r) return false;
  fee = *r;
  return true;
}

uint64_t closed_form_2hop_cp(const Cycle& c, const Snapshots& s) {
  if (c.n != 2) return 0;
  CpLeg l1, l2;
  if (!cp_leg(*s[0], c.legs[0].a_to_b, l1) || !cp_leg(*s[1], c.legs[1].a_to_b, l2)) return 0;
  // S = sqrt(n1 n2 ro1 ro2 ri1 ri2) * 2^16, from integer roots of the reserve products.
  u128 ab = isqrt_u128(l1.ro * l2.ro) * isqrt_u128(l1.ri * l2.ri);
  U256 s_scaled = U256::mul(ab, isqrt_u128((l1.n * l2.n) << 32));             // * 2^16
  U256 num = U256::mul(s_scaled.shr(16).lo128(), isqrt_u128((l1.d * l2.d) << 32));  // S * sqrt(d1 d2) * 2^16
  if (!s_scaled.shr(16).fits_u128()) return 0;
  num = num.shr(16);
  U256 a_dd = U256::mul(l1.ri * l2.ri, l1.d * l2.d);
  if (num.lte(a_dd)) return 0;  // no profitable size
  U256 sum = U256::mul(l2.d, l2.ri).add(U256::mul(l2.n, l1.ro));
  if (!sum.fits_u128()) return 0;
  U256 den = U256::mul(sum.lo128(), l1.n);
  if (den.is_zero()) return 0;
  U256 qq, rr;
  num.sub(a_dd).divmod(den, qq, rr);
  if (!qq.fits_u128() || qq.lo128() > UINT64_MAX) return UINT64_MAX;
  return uint64_t(qq.lo128());
}

uint64_t depth_bound(const Cycle& c, const Snapshots& s, uint32_t fraction_ppm) {
  u128 rate_before = ONE_X64;  // base units -> leg-i input units, Q64.64
  u128 bound = ~u128(0);
  for (int i = 0; i < c.n; ++i) {
    u128 depth = dex::input_depth(*s[i], c.legs[i].a_to_b);
    if (rate_before == 0) return 0;
    U256 qq, rr;
    U256::mul(depth, ONE_X64).divmod(U256::from_u128(rate_before), qq, rr);  // depth / rate, in base units
    u128 in_base = qq.fits_u128() ? qq.lo128() : ~u128(0);
    bound = std::min(bound, in_base / 1'000'000 * fraction_ppm);
    rate_before = mul_shr64_sat(rate_before, dex::spot_rate_x64(*s[i], c.legs[i].a_to_b));
  }
  return bound > UINT64_MAX ? UINT64_MAX : uint64_t(bound);
}

SizeResult size_cycle(const Cycle& c, const Snapshots& s, const Quoters& q, const FlashFeeFn& fee, const SizerConfig& cfg) {
  SizeResult best;
  if (cfg.x_max < cfg.min_trade || cfg.min_trade == 0) return best;
  uint64_t boundary_at = 0;  // smallest size seen to cross a boundary
  int evals = 0;
  struct Memo {
    uint64_t x;
    int64_t v;
  };
  Memo memo[64];
  int n_memo = 0;
  constexpr int64_t INVALID = INT64_MIN;
  auto eval = [&](uint64_t x) -> int64_t {
    x = std::clamp(x, cfg.min_trade, cfg.x_max);
    for (int i = 0; i < n_memo; ++i)
      if (memo[i].x == x) return memo[i].v;
    ++evals;
    int64_t v = INVALID;
    ChainQuote ch = quote_chain(c, s, q, x);
    uint64_t ff = 0;
    if (ch.boundary && (boundary_at == 0 || x < boundary_at)) boundary_at = x;
    switch (ch.fail) {
      case ChainQuote::quote_error: ++best.n_quote_error; best.last_err = ch.err; break;
      case ChainQuote::boundary_fail:
        ++best.n_boundary;
        if (ch.cache_gap) ++best.n_boundary_cache;
        break;
      case ChainQuote::partial_fill: ++best.n_partial; break;
      case ChainQuote::zero_out: ++best.n_zero_out; break;
      case ChainQuote::no_quoter: ++best.n_no_quoter; break;
      case ChainQuote::none: break;
    }
    if (ch.fail != ChainQuote::none) best.last_fail_leg = ch.fail_leg;
    if (ch.ok && !fee(x, ff)) ++best.n_fee;
    if (ch.ok && fee(x, ff)) {
      __int128 p = __int128(ch.final_out) - __int128(x) - __int128(ff);
      v = p > INT64_MAX ? INT64_MAX : p < INT64_MIN + 1 ? INT64_MIN + 1 : int64_t(p);
      best.any_clean = true;
      if (v > best.best_profit) {
        best.best_profit = v;
        best.best_x = x;
      }
      if (!best.ok || v > best.profit) {
        best.ok = true;
        best.x = x;
        best.profit = v;
        best.flash_fee = ff;
        best.chain = ch;
      }
    }
    if (n_memo < 64) memo[n_memo++] = {x, v};
    return v;
  };

  uint64_t seed = cfg.use_closed_form ? closed_form_2hop_cp(c, s) : 0;
  uint64_t lo, hi;
  if (seed > 0) {
    seed = std::clamp(seed, cfg.min_trade, cfg.x_max);
    lo = std::max(cfg.min_trade, seed / 2);
    hi = seed > cfg.x_max / 2 ? cfg.x_max : seed * 2;
    eval(seed);
  } else {
    // No closed form (3-hop or a Whirlpool leg): the useful size can sit anywhere between min_trade and x_max,
    // often orders of magnitude apart, so scan geometrically first (x min_trade * f^k, at most half the
    // budget), then refine with golden-section between the best probe's neighbours. Integers only.
    lo = cfg.min_trade;
    hi = cfg.x_max;
    int max_probes = std::max(2, cfg.max_evals / 2);
    u128 f = 4;
    auto probes_for = [&](u128 step) {
      int n = 1;
      for (u128 x = cfg.min_trade; x < cfg.x_max && n < 1000; x *= step) ++n;
      return n;
    };
    while (probes_for(f) > max_probes && f < (u128(1) << 32)) f *= 2;
    uint64_t best_probe = 0;
    int64_t best_v = INVALID;
    for (u128 x = cfg.min_trade;; x *= f) {
      uint64_t xx = x >= cfg.x_max ? cfg.x_max : uint64_t(x);
      int64_t v = eval(xx);
      if (v != INVALID && (best_probe == 0 || v > best_v)) {
        best_v = v;
        best_probe = xx;
      }
      if (xx == cfg.x_max) break;
    }
    if (best_probe) {
      lo = std::max<uint64_t>(cfg.min_trade, uint64_t(best_probe / f));
      hi = u128(best_probe) * f >= cfg.x_max ? cfg.x_max : uint64_t(u128(best_probe) * f);
    }
  }
  // Golden-section on integers: 0.381966 / 0.618034 interior points, one new evaluation per step.
  auto at = [](uint64_t a, uint64_t b, uint64_t ppm) { return a + uint64_t(u128(b - a) * ppm / 1'000'000); };
  uint64_t x1 = at(lo, hi, 381'966), x2 = at(lo, hi, 618'034);
  int64_t f1 = eval(x1), f2 = eval(x2);
  while (evals < cfg.max_evals && hi - lo > 2) {
    if (f1 >= f2) {
      hi = x2;
      x2 = x1;
      f2 = f1;
      x1 = at(lo, hi, 381'966);
      if (x1 >= x2) x1 = x2 > lo ? x2 - 1 : lo;
      f1 = eval(x1);
    } else {
      lo = x1;
      x1 = x2;
      f1 = f2;
      x2 = at(lo, hi, 618'034);
      if (x2 <= x1) x2 = x1 < hi ? x1 + 1 : hi;
      f2 = eval(x2);
    }
  }
  if (!best.ok && boundary_at > 0) {  // spec §6.4 step 4: one retry at 70% of the boundary size
    best.retried = true;
    eval(boundary_at / 10 * 7);
  }
  best.evals = evals;
  if (best.ok && best.profit <= 0) best.ok = false;
  return best;
}

}  // namespace arb::engine
