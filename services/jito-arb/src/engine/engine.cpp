#include "engine/engine.hpp"

#include <algorithm>
#include <cmath>

#include "core/programs.hpp"
#include "core/sys.hpp"
#include "core/u256.hpp"
#include "dex/dlmm.hpp"

namespace arb::engine {

namespace {

uint32_t ppm(double v) { return uint32_t(std::llround(std::max(0.0, v) * 1e6)); }

/** Mid price of `p` as Q64.64 units of `to` per unit of `from` (no fee): reserves or sqrt price. */
bool mid_x64(const PoolState& p, const Pubkey& from, u128& out) {
  bool a_to_b = p.mint_a == from;
  if (p.kind == DexKind::Dlmm) { out = dex::dlmm::mid(p, a_to_b); return out != 0; }
  if (is_constant_product(p.kind)) {
    uint64_t ri = a_to_b ? p.vault_a_amount : p.vault_b_amount, ro = a_to_b ? p.vault_b_amount : p.vault_a_amount;
    dex::cp_reserves(p, a_to_b, ri, ro);  // less fees owed where the program excludes them
    u128 rin = ri, rout = ro;
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
  // Twice the resync threshold: live mode refreshes a quiet pool by RPC once it lags resync_lag_ms, well
  // before the engine would call it stale.
  e.max_slot_lag = 2 * ms_to_slots(c, c.helius.resync_lag_ms);
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
  e.dispatch.cooldown_slots = int(ms_to_slots(c, c.engine.cooldown_ms));
  e.dispatch.max_send_lag_slots = int(ms_to_slots(c, c.engine.max_send_lag_ms));
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
  // Constant-product pools first: their vault balances are their depth. A concentrated-liquidity pool's vaults
  // can hold a stranded position at a price far from the market (one seen at ~370x off), so a Whirlpool is used
  // only when no constant-product pair is quotable, ranked by active liquidity (dex::input_depth).
  std::shared_ptr<const PoolState> best;
  std::pair<int, uint64_t> best_rank{-1, 0};
  for (uint32_t i = 0; i < store_.pool_count(); ++i) {
    auto p = store_.pool(i);
    if (!p || !p->quotable(cfg_.max_state_age_slots)) continue;
    bool pair = (p->mint_a == base && p->mint_b == mint::WSOL) || (p->mint_b == base && p->mint_a == mint::WSOL);
    if (!pair) continue;
    bool sol_is_a = p->mint_a == mint::WSOL;
    bool cp = is_constant_product(p->kind);
    std::pair<int, uint64_t> rank{cp ? 1 : 0,
                                  cp ? (sol_is_a ? p->vault_a_amount : p->vault_b_amount)
                                                                : dex::input_depth(*p, sol_is_a)};
    if (rank > best_rank) {
      best_rank = rank;
      best = p;
    }
  }
  u128 rate;
  if (!best || !mid_x64(*best, base, rate)) return false;
  prices_[base] = BasePrice{rate, true};
  return true;
}

StaleCause Engine::load(const Cycle& c, Snapshots& s, uint64_t slot, uint64_t* spread_mask) const {
  // Every leg is checked so a cycle stale only by spread can be told apart (spread on any leg, nothing else).
  StaleCause cause = StaleCause::fresh;
  *spread_mask = 0;
  for (int i = 0; i < c.n; ++i) {
    s[i] = store_.pool(c.legs[i].pool);
    const PoolState* p = s[i].get();
    if (!p || !p->complete) return StaleCause::incomplete;
    if (p->awaiting_push) return StaleCause::resync;
    if (slot > p->slot && slot - p->slot > cfg_.max_slot_lag) return StaleCause::lag;
    if (!p->quotable(cfg_.max_state_age_slots)) {
      cause = StaleCause::spread;
      *spread_mask |= uint64_t(1) << i;
    }
  }
  return cause;
}

bool Engine::x_max_for(const Cycle& c, const Snapshots& s, const BasePrice& bp, uint64_t& x_max, Reason& why) const {
  x_max = std::min(depth_bound(c, s, cfg_.max_input_fraction_ppm), bp.from_lamports_ceil(cfg_.max_trade_lamports));
  if (cfg_.flash) {
    auto it = reserves_.find(c.base);
    if (it == reserves_.end() || !it->second.enabled) {
      why = Reason::flash_disabled;
      return false;
    }
    x_max = std::min(x_max, it->second.available);
  } else {
    auto it = own_balance_.find(c.base);
    x_max = std::min(x_max, it == own_balance_.end() ? 0 : it->second);
  }
  return true;
}

void Engine::shadow_spread(uint32_t cycle, const Cycle& c, const Snapshots& s, uint64_t spread_mask) {
  // Shadow only: no dispatch, no decision record, no untradable memo, no sizer diagnostics.
  for (int i = 0; i < c.n; ++i) {
    const dex::IQuoter* q = q_.get(s[i]->kind);
    if (!q || !q->supports(*s[i])) return;
    auto it = untradable_.find((uint64_t(c.legs[i].pool) << 1) | uint64_t(c.legs[i].a_to_b));
    if (it != untradable_.end() && it->second == s[i]->seq) return;
  }
  if (spot_product_x64(c, s) < edge_x64_) return;
  ++n_.spread_edge;
  const BasePrice* bp = base_price(c.base);
  uint64_t x_max = 0;
  Reason why = Reason::none;
  bool priced = false;
  int64_t net = 0;
  if (bp && bp->known && x_max_for(c, s, *bp, x_max, why)) {
    FlashFeeFn fee;
    fee.flash = cfg_.flash;
    if (cfg_.flash) fee.fee_sf = reserves_.find(c.base)->second.flash_loan_fee_sf;
    SizerConfig sc;
    sc.min_trade = std::max<uint64_t>(1, bp->from_lamports_ceil(cfg_.min_trade_lamports));
    sc.x_max = x_max;
    sc.max_evals = cfg_.sizer_max_evals;
    if (sc.x_max >= sc.min_trade) {
      SizeResult r = size_cycle(c, s, q_, fee, sc);
      if (r.ok) {
        Pricing pr = price(r.x, r.profit, r.flash_fee, r.chain.cu, c.route_class, *bp, tips_, cfg_.costs);
        if (pr.ok) {
          priced = true;
          net = pr.net_lamports;
          ++n_.spread_priced;
          n_.spread_gross_lamports += pr.gross_lamports;
        }
      }
    }
  }
  for (int i = 0; i < c.n; ++i) {
    if (!(spread_mask >> i & 1)) continue;
    SpreadExample& e = spread_by_pool_[c.legs[i].pool];
    e.pool = c.legs[i].pool;
    ++e.edge_hits;
    if (priced) {
      ++e.priced;
      if (net > e.best_net) {
        e.best_net = net;
        e.best_cycle = cycle;
      }
    }
  }
}

std::vector<SpreadExample> Engine::take_spread_examples(size_t n) {
  std::vector<SpreadExample> v;
  v.reserve(spread_by_pool_.size());
  for (auto& [id, e] : spread_by_pool_) v.push_back(e);
  spread_by_pool_.clear();
  std::sort(v.begin(), v.end(), [](const SpreadExample& a, const SpreadExample& b) {
    return a.priced != b.priced ? a.priced > b.priced : a.edge_hits > b.edge_hits;
  });
  if (v.size() > n) v.resize(n);
  return v;
}

uint64_t Engine::reserve_available(const Pubkey& base) const {
  if (cfg_.flash) {
    auto it = reserves_.find(base);
    return it == reserves_.end() ? 0 : it->second.available;
  }
  auto it = own_balance_.find(base);
  return it == own_balance_.end() ? 0 : it->second;
}

bool Engine::evaluate(uint32_t cycle, uint64_t slot, Opportunity* out) {
  const Cycle& c = idx_.cycle(cycle);
  if (disp_.cooling(cycle, slot)) {
    ++n_.cooling;
    return false;
  }
  Snapshots s;
  uint64_t spread_mask = 0;
  if (StaleCause sc = load(c, s, slot, &spread_mask); sc != StaleCause::fresh) {
    ++n_.stale;
    switch (sc) {
      case StaleCause::incomplete: ++n_.stale_incomplete; break;
      case StaleCause::resync: ++n_.stale_resync; break;
      case StaleCause::lag: ++n_.stale_lag; break;
      case StaleCause::spread: ++n_.stale_spread; shadow_spread(cycle, c, s, spread_mask); break;
      case StaleCause::fresh: break;
    }
    return false;
  }
  for (int i = 0; i < c.n; ++i) {
    const dex::IQuoter* q = q_.get(s[i]->kind);
    if (!q || !q->supports(*s[i])) {
      ++n_.unsupported;
      return false;
    }
    if (!untradable_.empty()) {
      auto it = untradable_.find((uint64_t(c.legs[i].pool) << 1) | uint64_t(c.legs[i].a_to_b));
      if (it != untradable_.end()) {
        if (it->second == s[i]->seq) {
          ++n_.untradable;
          return false;
        }
        untradable_.erase(it);  // the pool changed: try it again
      }
    }
  }
  // A cycle whose x_max was below min_trade stays so until a leg's pool or the flash reserve changes.
  if (!no_room_.empty()) {
    auto it = no_room_.find(cycle);
    if (it != no_room_.end()) {
      // The flash reserve moves every slot: it only matters when it was what kept x_max below min_trade.
      bool same = !it->second.reserve_bound || reserve_available(c.base) < it->second.min_trade;
      for (int i = 0; i < c.n && same; ++i) same = it->second.seq[i] == s[i]->seq;
      if (same) {
        ++n_.no_room_memo;
        return false;
      }
      no_room_.erase(it);
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
  uint64_t x_max = 0;
  if (Reason why = Reason::none; !x_max_for(c, s, *bp, x_max, why)) return log_skip(why);
  FlashFeeFn fee;
  fee.flash = cfg_.flash;
  if (cfg_.flash) fee.fee_sf = reserves_.find(c.base)->second.flash_loan_fee_sf;
  SizerConfig sc;
  sc.min_trade = std::max<uint64_t>(1, bp->from_lamports_ceil(cfg_.min_trade_lamports));
  sc.x_max = x_max;
  sc.max_evals = cfg_.sizer_max_evals;
  if (sc.x_max < sc.min_trade) {
    // No size can trade: do not run the sizer, and skip this cycle until its inputs change.
    ++diag_.no_room;
    NoRoom& nr = no_room_[cycle];
    nr.min_trade = sc.min_trade;
    nr.reserve_bound = reserve_available(c.base) < sc.min_trade;
    for (int i = 0; i < c.n; ++i) nr.seq[i] = s[i]->seq;
    return log_skip(Reason::sizer_no_room);
  }
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
    else if (r.retried) {
      why = Reason::boundary;
      ++diag_.boundary;
      if (r.n_boundary_cache) ++diag_.boundary_cache;
    }
    else if (r.any_clean) { why = Reason::sizer_no_profit; ++diag_.no_profit; }
    else if (r.n_partial) { why = Reason::sizer_partial_fill; ++diag_.partial_fill; }
    else if (r.n_quote_error) {
      why = Reason::sizer_quote_error;
      ++diag_.quote_error;
      ++diag_.quote_error_by_code[size_t(r.last_err) & 31];
      // Every size failed on one leg with a program error: remember that pool and direction until it changes.
      if (r.last_fail_leg >= 0 && r.last_fail_leg < c.n) {
        const Leg& l = c.legs[r.last_fail_leg];
        untradable_[(uint64_t(l.pool) << 1) | uint64_t(l.a_to_b)] = s[size_t(r.last_fail_leg)]->seq;
      }
    }
    else if (r.n_zero_out) { why = Reason::sizer_zero_out; ++diag_.zero_out; }
    else if (r.n_boundary) {
      why = Reason::boundary;
      ++diag_.boundary;
      if (r.n_boundary_cache) ++diag_.boundary_cache;
    }
    else { why = Reason::sizer_failed; ++diag_.other; }
    // Every size ran out of the program's 3-array window on one leg (not a cache gap): the chain would fail
    // too, so that pool direction is skipped like a program error until its state changes.
    if (why == Reason::boundary && r.n_boundary_cache == 0 && !r.any_clean && r.last_fail_leg >= 0 && r.last_fail_leg < c.n) {
      const Leg& l = c.legs[r.last_fail_leg];
      untradable_[(uint64_t(l.pool) << 1) | uint64_t(l.a_to_b)] = s[size_t(r.last_fail_leg)]->seq;
    }
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
