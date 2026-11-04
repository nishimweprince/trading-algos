#include "engine/engine.hpp"

#include <algorithm>
#include <cmath>

#include "core/programs.hpp"
#include "core/sys.hpp"
#include "core/u256.hpp"

namespace arb::engine {

namespace {

uint32_t ppm(double v) { return uint32_t(std::llround(std::max(0.0, v) * 1e6)); }

/** Mid price of `p` as Q64.64 units of `to` per unit of `from` (no fee): reserves or sqrt price. */
bool mid_x64(const PoolState& p, const Pubkey& from, u128& out) {
  bool a_to_b = p.mint_a == from;
  if (p.kind == DexKind::RaydiumV4) {
    u128 rin = a_to_b ? p.vault_a_amount : p.vault_b_amount, rout = a_to_b ? p.vault_b_amount : p.vault_a_amount;
    if (rin == 0) return false;
    out = (rout << 64) / rin;
    return true;
  }
  if (p.kind == DexKind::Whirlpool) {
    u128 s = p.wp.sqrt_price;
    if (s == 0) return false;
    U256 sq = U256::mul(s, s);  // price b/a, Q128.128
    if (a_to_b) {
      U256 r = sq.shr(64);
      out = r.fits_u128() ? r.lo128() : ~u128(0);
    } else {
      U256 q, rem;
      U256::from_words(0, 1, 0, 0).shl64().shl64().divmod(sq, q, rem);  // 2^192 / s^2
      out = q.fits_u128() ? q.lo128() : ~u128(0);
    }
    return true;
  }
  return false;
}

}  // namespace

EngineConfig engine_config(const Config& c) {
  EngineConfig e;
  e.max_state_age_slots = uint64_t(c.engine.max_state_age_slots);
  // Twice the resync threshold: live mode refreshes a quiet pool by RPC once it lags resync_lag_slots, well
  // before the engine would call it stale.
  e.max_slot_lag = 2 * uint64_t(c.helius.resync_lag_slots);
  e.min_edge_ppm = ppm(c.engine.min_spot_edge);
  e.max_input_fraction_ppm = ppm(c.engine.max_input_fraction);
  e.max_trade_lamports = c.engine.max_trade_lamports;
  e.min_trade_lamports = c.engine.min_trade_lamports;
  e.sizer_max_evals = c.engine.sizer_max_evals;
  e.flash = c.mode.funding_mode == "flash";
  e.costs.base_fee_lamports = c.engine.base_fee_lamports;
  e.costs.cu_price_micro_lamports = c.engine.cu_price_micro_lamports;
  e.costs.min_net_lamports = c.engine.min_net_lamports;
  e.costs.min_ev_lamports = c.engine.min_ev_lamports;
  e.costs.tip_min_lamports = c.jito.tip_min_lamports;
  e.costs.tip_max_lamports = c.jito.tip_max_lamports;
  e.costs.cu_overhead = c.engine.cu_overhead;
  e.costs.cu_headroom_ppm = ppm(c.engine.cu_headroom);
  e.costs.max_route_cu = c.engine.max_route_cu;
  e.dispatch.max_bundles_per_slot = c.engine.max_bundles_per_slot;
  e.dispatch.cooldown_slots = c.engine.cooldown_slots;
  e.dispatch.max_send_lag_slots = c.engine.max_send_lag_slots;
  return e;
}

TipPolicy::Params tip_params(const Config& c) {
  TipPolicy::Params p;
  p.alpha_init_ppm = ppm(c.engine.alpha_init);
  p.alpha_min_ppm = ppm(c.engine.alpha_min);
  p.alpha_max_ppm = ppm(c.engine.alpha_max);
  p.step_ppm = ppm(c.engine.alpha_step);
  p.target_land_ppm = ppm(c.engine.target_land_rate);
  p.window = uint32_t(c.engine.alpha_window);
  p.p_land_init_ppm = ppm(c.engine.p_land_init);
  return p;
}

Engine::Engine(const StateStore& store, const CycleIndex& idx, Quoters q, EngineConfig cfg, TipPolicy& tips)
    : store_(store), idx_(idx), q_(q), cfg_(cfg), tips_(tips), disp_(cfg.dispatch), edge_x64_(edge_threshold_x64(cfg.min_edge_ppm)) {
  prices_[mint::WSOL] = BasePrice{};
}

const BasePrice* Engine::base_price(const Pubkey& mint) const {
  auto it = prices_.find(mint);
  return it == prices_.end() ? nullptr : &it->second;
}

bool Engine::refresh_base_price_from_pools(const Pubkey& base) {
  if (base == mint::WSOL) return true;
  std::shared_ptr<const PoolState> best;
  uint64_t best_depth = 0;
  for (uint32_t i = 0; i < store_.pool_count(); ++i) {
    auto p = store_.pool(i);
    if (!p || !p->quotable(cfg_.max_state_age_slots)) continue;
    bool pair = (p->mint_a == base && p->mint_b == mint::WSOL) || (p->mint_b == base && p->mint_a == mint::WSOL);
    if (!pair) continue;
    uint64_t depth = p->mint_a == mint::WSOL ? p->vault_a_amount : p->vault_b_amount;
    if (depth > best_depth) {
      best_depth = depth;
      best = p;
    }
  }
  u128 rate;
  if (!best || !mid_x64(*best, base, rate)) return false;
  prices_[base] = BasePrice{rate, true};
  return true;
}

bool Engine::load(const Cycle& c, Snapshots& s, uint64_t slot) const {
  for (int i = 0; i < c.n; ++i) {
    s[i] = store_.pool(c.legs[i].pool);
    if (!s[i] || !s[i]->quotable(cfg_.max_state_age_slots)) return false;
    if (slot > s[i]->slot && slot - s[i]->slot > cfg_.max_slot_lag) return false;
  }
  return true;
}

bool Engine::evaluate(uint32_t cycle, uint64_t slot, Opportunity* out) {
  const Cycle& c = idx_.cycle(cycle);
  if (disp_.cooling(cycle, slot)) {
    ++n_.cooling;
    return false;
  }
  Snapshots s;
  if (!load(c, s, slot)) {
    ++n_.stale;
    return false;
  }
  for (int i = 0; i < c.n; ++i) {
    const dex::IQuoter* q = q_.get(s[i]->kind);
    if (!q || !q->supports(*s[i])) {
      ++n_.unsupported;
      return false;
    }
  }
  u128 prod = spot_product_x64(c, s);
  if (prod < edge_x64_) return false;
  ++n_.prefilter_pass;
  Opportunity o;
  o.cycle = cycle;
  o.slot = slot;
  o.state_slot = UINT64_MAX;
  for (int i = 0; i < c.n; ++i) {
    o.leg_slot[i] = s[i]->slot;
    o.leg_seq[i] = s[i]->seq;
    o.state_slot = std::min(o.state_slot, s[i]->slot);
  }
  auto log_skip = [&](Reason r) {
    ++n_.skipped;
    if (skip_) skip_(o, r);
    return false;
  };
  const BasePrice* bp = base_price(c.base);
  if (!bp || !bp->known) return log_skip(Reason::base_price_unknown);

  // x_max (§6.4 step 1): flash liquidity (or own balance), depth fraction, max_trade.
  uint64_t x_max = depth_bound(c, s, cfg_.max_input_fraction_ppm);
  uint64_t max_trade = bp->from_lamports_ceil(cfg_.max_trade_lamports);
  x_max = std::min(x_max, max_trade);
  FlashFeeFn fee;
  fee.flash = cfg_.flash;
  if (cfg_.flash) {
    auto it = reserves_.find(c.base);
    if (it == reserves_.end() || !it->second.enabled) return log_skip(Reason::flash_disabled);
    fee.fee_sf = it->second.flash_loan_fee_sf;
    x_max = std::min(x_max, it->second.available);
  } else {
    auto it = own_balance_.find(c.base);
    x_max = std::min(x_max, it == own_balance_.end() ? 0 : it->second);
  }
  SizerConfig sc;
  sc.min_trade = std::max<uint64_t>(1, bp->from_lamports_ceil(cfg_.min_trade_lamports));
  sc.x_max = x_max;
  sc.max_evals = cfg_.sizer_max_evals;
  SizeResult r = size_cycle(c, s, q_, fee, sc);
  ++n_.sized;
  uint64_t tnow = now_ns();
  int64_t edge_ppm = int64_t((__int128(prod) - (__int128(1) << 64)) * 1'000'000 >> 64);
  edge_ppm_.add(tnow, edge_ppm);
  for (int i = 0; i < c.n; ++i)
    if (s[i]->kind == DexKind::Whirlpool && s[i]->wp.liquidity == 0) {
      ++diag_.zero_liquidity_leg;
      break;
    }
  if (!r.ok) {
    Reason why;
    if (sc.x_max < sc.min_trade) { why = Reason::sizer_no_room; ++diag_.no_room; }
    else if (r.retried) { why = Reason::boundary; ++diag_.boundary; }
    else if (r.any_clean) { why = Reason::sizer_no_profit; ++diag_.no_profit; }
    else if (r.n_partial) { why = Reason::sizer_partial_fill; ++diag_.partial_fill; }
    else if (r.n_quote_error) {
      why = Reason::sizer_quote_error;
      ++diag_.quote_error;
      ++diag_.quote_error_by_code[size_t(r.last_err) & 31];
    }
    else if (r.n_zero_out) { why = Reason::sizer_zero_out; ++diag_.zero_out; }
    else if (r.n_boundary) { why = Reason::boundary; ++diag_.boundary; }
    else { why = Reason::sizer_failed; ++diag_.other; }
    if (why == Reason::sizer_no_profit && r.best_x)
      failed_profit_ppm_.add(tnow, int64_t(__int128(r.best_profit) * 1'000'000 / __int128(r.best_x)));
    SizerExample& ex = examples_[size_t(why) & 31];
    if (!ex.set || edge_ppm > ex.edge_ppm)
      ex = SizerExample{true, cycle, slot, edge_ppm, sc.x_max, r.best_x, r.any_clean ? r.best_profit : 0, why, sc.min_trade,
                        r.last_err, r.last_fail_leg};
    return log_skip(why);
  }
  o.x = r.x;
  o.final_out = r.chain.final_out;
  o.flash_fee = r.flash_fee;
  o.profit_base = r.profit;
  for (int i = 0; i < c.n; ++i) o.leg_out[i] = r.chain.outs[i];
  o.pr = price(r.x, r.profit, r.flash_fee, r.chain.cu, c.route_class, *bp, tips_, cfg_.costs);
  if (!o.pr.ok) return log_skip(o.pr.reject);
  ++n_.candidates;
  if (out) *out = o;
  disp_.push(o);
  return true;
}

void Engine::on_pool_update(uint32_t pool, uint64_t slot) {
  ++n_.updates;
  for (uint32_t c : idx_.cycles_of(pool)) {
    ++n_.cycles_seen;
    evaluate(c, slot);
  }
  dispatch_best(slot);
}

void Engine::dispatch_best(uint64_t current_slot) {
  if (disp_.pending() == 0) return;
  disp_.dispatch(
      current_slot, idx_,
      [&](const Opportunity& o) {
        ++n_.sent;
        if (send_) send_(o);
      },
      [&](const Opportunity& o, Reason r) {
        ++n_.skipped;
        if (skip_) skip_(o, r);
      });
}

}  // namespace arb::engine
