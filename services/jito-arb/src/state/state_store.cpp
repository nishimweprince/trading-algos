#include "state/state_store.hpp"

#include <algorithm>
#include <stdexcept>
#include <string>

#include "core/crypto.hpp"
#include "core/programs.hpp"

namespace arb {

const char* dex_name(DexKind k) {
  switch (k) {
    case DexKind::RaydiumV4: return "raydium_v4";
    case DexKind::Whirlpool: return "whirlpool";
    case DexKind::Cpmm: return "cpmm";
    case DexKind::Dlmm: return "dlmm";
    case DexKind::Clmm: return "clmm";
    case DexKind::PumpSwap: return "pumpswap";
  }
  return "?";
}

std::optional<DexKind> dex_kind_from_name(std::string_view name) {
  for (DexKind k : {DexKind::RaydiumV4, DexKind::Whirlpool, DexKind::Cpmm, DexKind::Dlmm, DexKind::Clmm, DexKind::PumpSwap})
    if (name == dex_name(k)) return k;
  return std::nullopt;
}

const Pubkey* dex_program(DexKind k) {
  switch (k) {
    case DexKind::RaydiumV4: return &prog::RAYDIUM_V4;
    case DexKind::Whirlpool: return &prog::WHIRLPOOL;
    case DexKind::Cpmm: return &prog::RAYDIUM_CPMM;
    case DexKind::PumpSwap: return &prog::PUMPSWAP;
    case DexKind::Clmm: return &prog::RAYDIUM_CLMM;
    default: return nullptr;
  }
}

namespace {

/** The account kind a pool's own account decodes as. */
AccountKind pool_account_kind(DexKind k) {
  switch (k) {
    case DexKind::RaydiumV4: return AccountKind::RaydiumPool;
    case DexKind::Whirlpool: return AccountKind::Whirlpool;
    case DexKind::Cpmm: return AccountKind::CpmmPool;
    case DexKind::PumpSwap: return AccountKind::PumpPool;
    case DexKind::Clmm: return AccountKind::ClmmPool;
    default: throw std::invalid_argument(std::string("state store: unsupported dex ") + dex_name(k));
  }
}

}  // namespace

bool PoolState::quotable(uint64_t max_age) const {
  if (!complete || awaiting_push) return false;
  uint64_t lo = std::min({pool_slot, vault_a_slot, vault_b_slot});
  uint64_t hi = std::max({pool_slot, vault_a_slot, vault_b_slot});
  return hi - lo <= max_age;
}

StateStore::StateStore() = default;

StateStore::AccountEntry& StateStore::entry(const Pubkey& k, AccountKind kind) {
  auto [it, created] = accounts_.try_emplace(k);
  if (created) it->second.kind = kind;
  return it->second;
}

uint32_t StateStore::add_pool(const PoolDef& def) {
  if (auto it = pool_index_.find(def.address); it != pool_index_.end()) return it->second;
  pool_account_kind(def.kind);  // throws for a kind this build cannot decode, before anything is registered
  uint32_t id = uint32_t(pools_.size());
  auto slot = std::make_unique<PoolSlot>();
  slot->def = def;
  pools_.push_back(std::move(slot));
  pool_index_[def.address] = id;
  entry(def.address, pool_account_kind(def.kind)).pools.push_back(id);
  entry(def.vault_a, AccountKind::Vault).pools.push_back(id);
  entry(def.vault_b, AccountKind::Vault).pools.push_back(id);
  entry(def.mint_a, AccountKind::Mint).pools.push_back(id);
  entry(def.mint_b, AccountKind::Mint).pools.push_back(id);
  if (!is_zero(def.config))
    entry(def.config, def.kind == DexKind::PumpSwap ? AccountKind::PumpGlobalConfig
                      : def.kind == DexKind::Clmm   ? AccountKind::ClmmConfig
                                                    : AccountKind::CpmmConfig)
        .pools.push_back(id);
  if (def.kind == DexKind::Clmm)
    entry(state::clmm_bitmap_extension_pda(def.address), AccountKind::ClmmBitmapExtension).pools.push_back(id);
  if (!is_zero(def.fee_config)) entry(def.fee_config, AccountKind::PumpFeeConfig).pools.push_back(id);
  for (auto& [start, key] : def.tick_arrays) {
    AccountEntry& e = entry(key, def.kind == DexKind::Clmm ? AccountKind::ClmmTickArray : AccountKind::TickArray);
    e.tick_start = start;
    e.pools.push_back(id);
  }
  rebuild(id);
  return id;
}

void StateStore::add_reserve(const Pubkey& reserve) { entry(reserve, AccountKind::KaminoReserve); }

std::optional<uint32_t> StateStore::pool_id(const Pubkey& address) const {
  auto it = pool_index_.find(address);
  if (it == pool_index_.end()) return std::nullopt;
  return it->second;
}

std::shared_ptr<const state::KaminoReserve> StateStore::reserve(const Pubkey& key) const {
  auto it = accounts_.find(key);
  return it == accounts_.end() ? nullptr : it->second.reserve;
}

std::shared_ptr<const state::Mint> StateStore::mint(const Pubkey& key) const {
  auto it = accounts_.find(key);
  return it == accounts_.end() ? nullptr : it->second.mint;
}

std::vector<Pubkey> StateStore::subscribed_keys() const {
  std::vector<Pubkey> out;
  out.reserve(accounts_.size());
  for (auto& [k, e] : accounts_) out.push_back(k);
  std::sort(out.begin(), out.end());
  return out;
}

void StateStore::mark_suspect(const Pubkey& key) {
  auto it = accounts_.find(key);
  if (it == accounts_.end() || it->second.awaiting_push) return;
  it->second.awaiting_push = true;
  for (uint32_t id : it->second.pools) rebuild(id);
}

bool StateStore::apply(const AccountUpdate& u) {
  auto it = accounts_.find(u.key);
  if (it == accounts_.end()) return true;  // not ours (a stale subscription)
  AccountEntry& e = it->second;
  // Per-account ordering: never go back in (slot, write_version).
  if (e.seen && (u.slot < e.slot || (u.slot == e.slot && u.write_version && u.write_version < e.write_version))) return true;
  updates_.fetch_add(1, std::memory_order_relaxed);
  if (u.slot > newest_slot_.load(std::memory_order_relaxed)) newest_slot_.store(u.slot, std::memory_order_relaxed);
  bool ok = true;
  if (u.missing) {
    e.missing = true;
    e.seen = true;
  } else {
    switch (e.kind) {
      case AccountKind::RaydiumPool: {
        auto r = state::decode_raydium_v4(u.data, u.len, u.owner);
        if (r) e.pool_data = *r; else ok = false;
        break;
      }
      case AccountKind::Whirlpool: {
        auto r = state::decode_whirlpool(u.data, u.len, u.owner);
        if (r) e.pool_data = *r; else ok = false;
        break;
      }
      case AccountKind::CpmmPool: {
        auto r = state::decode_cpmm_pool(u.data, u.len, u.owner);
        if (r) e.pool_data = *r; else ok = false;
        break;
      }
      case AccountKind::CpmmConfig: {
        auto r = state::decode_cpmm_config(u.data, u.len, u.owner);
        if (r) e.pool_data = *r; else ok = false;
        break;
      }
      case AccountKind::ClmmPool: {
        auto r = state::decode_clmm_pool(u.data, u.len, u.owner);
        if (r) e.pool_data = *r; else ok = false;
        break;
      }
      case AccountKind::ClmmConfig: {
        auto r = state::decode_clmm_config(u.data, u.len, u.owner);
        if (r) e.pool_data = *r; else ok = false;
        break;
      }
      case AccountKind::ClmmBitmapExtension: {
        auto r = state::decode_clmm_bitmap_extension(u.data, u.len, u.owner);
        if (r && state::clmm_bitmap_extension_pda(r->pool) == u.key)
          e.clmm_bitmap = std::make_shared<const state::ClmmBitmapExtension>(*r);
        else ok = false;
        break;
      }
      case AccountKind::ClmmTickArray: {
        auto r = state::decode_clmm_tick_array(u.data, u.len, u.owner);
        if (r && r->start_tick_index == e.tick_start && state::clmm_tick_array_pda(r->pool, e.tick_start) == u.key) e.clmm_tick_array = std::make_shared<const state::ClmmTickArray>(*r);
        else ok = false;
        break;
      }
      case AccountKind::PumpPool: {
        auto r = state::decode_pump_pool(u.data, u.len, u.owner);
        if (r) e.pool_data = *r; else ok = false;
        break;
      }
      case AccountKind::PumpGlobalConfig: {
        auto r = state::decode_pump_global_config(u.data, u.len, u.owner);
        if (r) e.pool_data = *r; else ok = false;
        break;
      }
      case AccountKind::PumpFeeConfig: {
        auto r = state::decode_pump_fee_config(u.data, u.len, u.owner);
        if (r) e.pump_fees = std::make_shared<const state::PumpFeeConfig>(std::move(*r)); else ok = false;
        break;
      }
      case AccountKind::Vault: {
        auto r = state::decode_token_account(u.data, u.len, u.owner);
        if (r) e.pool_data = *r; else ok = false;
        break;
      }
      case AccountKind::TickArray: {
        auto r = state::decode_tick_array(u.data, u.len, u.owner);
        if (r && r->start_tick_index == e.tick_start) e.tick_array = std::make_shared<const state::TickArray>(*r);
        else ok = false;
        break;
      }
      case AccountKind::Mint: {
        auto r = state::decode_mint(u.data, u.len, u.owner);
        if (r) e.mint = std::make_shared<const state::Mint>(*r); else ok = false;
        break;
      }
      case AccountKind::KaminoReserve: {
        auto r = state::decode_kamino_reserve(u.data, u.len, u.owner);
        if (r) e.reserve = std::make_shared<const state::KaminoReserve>(*r); else ok = false;
        break;
      }
      case AccountKind::Wallet: break;
    }
    if (ok) {
      e.missing = false;
      e.seen = true;
    }
  }
  if (!ok) {
    // A malformed CLMM update must revoke its previously published decoded value.
    if (e.kind == AccountKind::ClmmBitmapExtension || e.kind == AccountKind::ClmmTickArray ||
        e.kind == AccountKind::ClmmPool || e.kind == AccountKind::ClmmConfig) {
      e.seen = true;
      e.missing = true;
      e.slot = u.slot;
      e.write_version = u.write_version;
      for (uint32_t id : e.pools) { rebuild(id); changed_.push(id); }
    }
    decode_failures_.fetch_add(1, std::memory_order_relaxed);
    return false;
  }
  e.slot = u.slot;
  e.write_version = u.write_version;
  e.awaiting_push = u.from_resync;
  for (uint32_t id : e.pools) {
    rebuild(id);
    changed_.push(id);  // a full queue drops the wake-up; the snapshot itself is still current
  }
  return true;
}

void StateStore::rebuild(uint32_t id) {
  const PoolDef& d = pools_[id]->def;
  auto s = std::make_shared<PoolState>();
  s->kind = d.kind;
  s->address = d.address;
  s->id = id;
  s->mint_a = d.mint_a;
  s->mint_b = d.mint_b;
  s->vault_a = d.vault_a;
  s->vault_b = d.vault_b;
  bool complete = true;
  auto find = [&](const Pubkey& k) -> const AccountEntry* {
    auto it = accounts_.find(k);
    return it == accounts_.end() ? nullptr : &it->second;
  };
  const AccountEntry* pool = find(d.address);
  if (pool && pool->seen && !pool->missing) {
    switch (d.kind) {
      case DexKind::RaydiumV4:
        if (auto* r = std::get_if<state::RaydiumV4>(&pool->pool_data)) s->ray = *r; else complete = false;
        break;
      case DexKind::Whirlpool:
        if (auto* w = std::get_if<state::Whirlpool>(&pool->pool_data)) s->wp = *w; else complete = false;
        break;
      case DexKind::Cpmm: {
        if (auto* c = std::get_if<state::CpmmPool>(&pool->pool_data)) s->cpmm = *c; else complete = false;
        const AccountEntry* cfg = find(d.config);
        const state::CpmmConfig* cc = cfg && cfg->seen && !cfg->missing ? std::get_if<state::CpmmConfig>(&cfg->pool_data) : nullptr;
        if (cc) s->cpmm_cfg = *cc; else complete = false;
        break;
      }
      case DexKind::Clmm: {
        if (auto* c = std::get_if<state::ClmmPool>(&pool->pool_data)) {
          s->clmm = *c;
          if (c->amm_config != d.config || c->token_mint_0 != d.mint_a || c->token_mint_1 != d.mint_b ||
              c->token_vault_0 != d.vault_a || c->token_vault_1 != d.vault_b) complete = false;
        } else complete = false;
        const AccountEntry* cfg = find(d.config);
        const state::ClmmConfig* cc = cfg && cfg->seen && !cfg->missing ? std::get_if<state::ClmmConfig>(&cfg->pool_data) : nullptr;
        if (cc && cc->tick_spacing == s->clmm.tick_spacing) {
          s->clmm_cfg = *cc;
          if (cfg->awaiting_push) s->awaiting_push = true;
        } else complete = false;
        const AccountEntry* bm = find(state::clmm_bitmap_extension_pda(d.address));
        if (bm && bm->seen && !bm->missing) {
          s->clmm_bitmap = bm->clmm_bitmap;
          if (bm->awaiting_push) s->awaiting_push = true;
        }
        break;
      }
      case DexKind::PumpSwap: {
        if (auto* pp = std::get_if<state::PumpPool>(&pool->pool_data)) {
          s->pump = *pp;
          PoolSlot& ps = *pools_[id];
          if (ps.pump_canonical < 0)  // fixed for the pool's life: its creator and base mint never change
            ps.pump_canonical = pp->creator == pda({std::string_view("pool-authority"), pp->base_mint}, prog::PUMP) ? 1 : 0;
          s->pump_canonical = ps.pump_canonical == 1;
        } else {
          complete = false;
        }
        const AccountEntry* gc = find(d.config);
        const state::PumpGlobalConfig* g = gc && gc->seen && !gc->missing ? std::get_if<state::PumpGlobalConfig>(&gc->pool_data) : nullptr;
        if (g) s->pump_cfg = *g; else complete = false;
        const AccountEntry* fc = find(d.fee_config);
        s->pump_fees = fc && fc->seen && !fc->missing ? fc->pump_fees : nullptr;
        if (!s->pump_fees) complete = false;
        break;
      }
      default:
        complete = false;
    }
    s->pool_slot = pool->slot;
    if (pool->awaiting_push) s->awaiting_push = true;
    s->write_version = pool->write_version;
  } else {
    complete = false;
  }
  auto vault = [&](const Pubkey& k, uint64_t& amount, uint64_t& slot) {
    const AccountEntry* v = find(k);
    const state::TokenAccount* t = v && v->seen && !v->missing ? std::get_if<state::TokenAccount>(&v->pool_data) : nullptr;
    if (!t) { complete = false; return; }
    amount = t->amount;
    slot = v->slot;
    if (v->awaiting_push) s->awaiting_push = true;
  };
  vault(d.vault_a, s->vault_a_amount, s->vault_a_slot);
  vault(d.vault_b, s->vault_b_amount, s->vault_b_slot);
  const AccountEntry* ma = find(d.mint_a);
  const AccountEntry* mb = find(d.mint_b);
  s->mint_a_info = ma ? ma->mint : nullptr;
  s->mint_b_info = mb ? mb->mint : nullptr;
  if (d.kind == DexKind::Clmm) {
    if (!ma || !ma->seen || ma->missing) s->mint_a_info.reset();
    if (!mb || !mb->seen || mb->missing) s->mint_b_info.reset();
    if ((ma && ma->awaiting_push) || (mb && mb->awaiting_push)) s->awaiting_push = true;
  }
  if (!s->mint_a_info || !s->mint_b_info) complete = false;
  for (auto& [start, key] : d.tick_arrays) {
    if (s->n_ticks >= MAX_TICK_ARRAYS) break;
    TickArraySlot& t = s->ticks[s->n_ticks++];
    t.start = start;
    t.key = key;
    const AccountEntry* e = find(key);
    if (e && e->seen) {
      if (e->missing) t.state = TickArraySlot::Missing;
      else if (e->tick_array) { t.state = TickArraySlot::Loaded; t.array = e->tick_array; }
      else if (e->clmm_tick_array) {
        t.state = TickArraySlot::Loaded; t.clmm = e->clmm_tick_array;
        if (e->awaiting_push) s->awaiting_push = true;
      }
    }
  }
  s->slot = std::max({s->pool_slot, s->vault_a_slot, s->vault_b_slot});
  s->complete = complete;
  s->seq = ++seq_;
  pools_[id]->snap.store(std::move(s));
}

}  // namespace arb
