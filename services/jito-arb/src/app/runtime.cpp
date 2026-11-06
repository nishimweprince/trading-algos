#include "app/runtime.hpp"

#include <cstring>

#include "core/base58.hpp"
#include "feed/frames.hpp"

namespace arb::app {

obs::DecisionRecord make_record(const StateStore& store, const engine::Cycle& c, const engine::Opportunity& o, int64_t wall_ms) {
  obs::DecisionRecord r;
  r.hops = c.n;
  r.route_class = c.route_class;
  r.cycle_id = c.id;
  r.wall_ms = wall_ms;
  r.slot = o.slot;
  r.x = o.x;
  r.final_out = o.final_out;
  r.flash_fee = o.flash_fee;
  r.profit_base = o.profit_base;
  r.gross_lamports = o.pr.gross_lamports;
  r.tip_lamports = o.pr.tip_lamports;
  r.fee_lamports = o.pr.fee_lamports;
  r.net_lamports = o.pr.net_lamports;
  r.ev_lamports = o.pr.ev_lamports;
  r.min_out = o.pr.min_out;
  r.cu_limit = o.pr.cu_limit;
  r.p_land_ppm = o.pr.p_land_ppm;
  for (int i = 0; i < c.n; ++i) {
    const PoolDef& d = store.def(c.legs[i].pool);
    r.dex[i] = uint8_t(d.kind);
    if (c.legs[i].a_to_b) r.a_to_b_mask |= uint8_t(1u << i);
    r.leg_out[i] = o.leg_out[i];
    r.state_slot[i] = o.leg_slot[i];
    r.state_seq[i] = o.leg_seq[i];
    std::memcpy(r.pools[i], d.address.data(), 32);
  }
  std::memcpy(r.base, c.base.data(), 32);
  return r;
}

namespace {

engine::Quoters make_quoters(const Config& cfg, const dex::IQuoter& ray, const dex::IQuoter& wp, const dex::IQuoter& cpmm,
                             const dex::IQuoter& pump) {
  engine::Quoters q;
  if (cfg.dex.raydium_v4_enabled) q.by_kind[uint8_t(DexKind::RaydiumV4)] = &ray;
  if (cfg.dex.whirlpool_enabled) q.by_kind[uint8_t(DexKind::Whirlpool)] = &wp;
  if (cfg.dex.cpmm_enabled) q.by_kind[uint8_t(DexKind::Cpmm)] = &cpmm;
  if (cfg.dex.pumpswap_enabled) q.by_kind[uint8_t(DexKind::PumpSwap)] = &pump;
  return q;
}

}  // namespace

Runtime::Runtime(const Config& cfg, const PoolsFile& pools)
    : cfg_(cfg), tips_(engine::tip_params(cfg)), ray_q_(cfg.dex.raydium_v4_cu), wp_q_(cfg.dex.whirlpool_cu), cpmm_q_(cfg.dex.cpmm_cu), pump_q_(cfg.dex.pumpswap_cu),
      handler_(store_, clock_) {
  for (const PoolDef& d : pools.pools) store_.add_pool(d);
  reserves_ = pools.reserves;
  for (const std::string& r : cfg.flash.reserves) reserves_.push_back(parse_pubkey(r));
  for (const Pubkey& r : reserves_) store_.add_reserve(r);
  engine::GraphConfig g;
  for (const std::string& m : cfg.engine.base_mints) g.base_mints.push_back(parse_pubkey(m));
  g.max_hops = cfg.engine.max_hops;
  g.max_cycles_per_pool = cfg.engine.max_cycles_per_pool;
  std::vector<PoolDef> defs;
  for (uint32_t i = 0; i < store_.pool_count(); ++i) defs.push_back(store_.def(i));
  idx_.build(defs, pools.tvl_sol, g);
  engine_ = std::make_unique<engine::Engine>(store_, idx_, make_quoters(cfg, ray_q_, wp_q_, cpmm_q_, pump_q_), engine::engine_config(cfg), tips_);
  engine_->on_skip([this](const engine::Opportunity& o, engine::Reason why) {
    if (!log_) return;
    obs::DecisionRecord r = make_record(store_, idx_.cycle(o.cycle), o, wall_ms_);
    r.action = uint8_t(obs::Action::Skipped);
    r.reason = uint8_t(why);
    if (log_->write(r)) ++stats_.records;
  });
  engine_->on_send([this](const engine::Opportunity& o) {
    ++stats_.sent;
    const engine::Cycle& c = idx_.cycle(o.cycle);
    obs::DecisionRecord r = make_record(store_, c, o, wall_ms_);
    r.action = uint8_t(obs::Action::DryRun);
    if (send_hook_) send_hook_(o, c, r);
    if (log_ && log_->write(r)) ++stats_.records;
  });
}

void Runtime::refresh_slot_inputs(uint64_t slot) {
  // Once per slot, off the per-update path: flash reserves (fee and liquidity, live from the feed) and
  // non-SOL base prices from the deepest SOL pair.
  if (slot == inputs_slot_) return;
  inputs_slot_ = slot;
  for (const Pubkey& k : reserves_) {
    auto r = store_.reserve(k);
    if (!r) continue;
    auto it = reserve_cache_.find(k);
    if (it == reserve_cache_.end() || it->second.lending_market != r->lending_market)
      it = reserve_cache_.insert_or_assign(k, flash::make_reserve(k, *r, slot)).first;
    flash::FlashReserve& f = it->second;  // live fields: fee, liquidity, status
    f.flash_loan_fee_sf = r->flash_loan_fee_sf;
    f.available = r->available_amount;
    f.enabled = r->flash_enabled();
    f.slot = slot;
    engine_->set_flash_reserve(r->liquidity_mint, f);
  }
  for (const std::string& m : cfg_.engine.base_mints) engine_->refresh_base_price_from_pools(parse_pubkey(m));
}

void Runtime::on_frame(const uint8_t* data, size_t len, uint64_t recv_ns, int64_t wall_ms) {
  ++stats_.frames;
  wall_ms_ = wall_ms;
  frame_recv_ns_ = recv_ns;
  if (!handler_.on_frame(data, len, recv_ns)) ++stats_.bad_frames;
  uint32_t id;
  bool refreshed = false;
  while (store_.pop_changed(id)) {
    auto snap = store_.pool(id);
    uint64_t slot = std::max(clock_.slot.load(std::memory_order_relaxed), snap ? snap->slot : 0);
    if (!refreshed) {
      refresh_slot_inputs(slot);
      refreshed = true;
    }
    ++stats_.updates;
    engine_->on_pool_update(id, slot);
  }
}

bool Runtime::replay_file(const std::string& path) {
  feed::FrameReader rd;
  if (!rd.open(path)) return false;
  feed::Frame f;
  while (rd.next(f)) on_frame(f.data.data(), f.data.size(), f.recv_ns, f.wall_ms);
  return true;
}

}  // namespace arb::app
