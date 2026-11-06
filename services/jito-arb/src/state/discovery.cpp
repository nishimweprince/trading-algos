#include "state/discovery.hpp"

#include <simdjson.h>

#include <algorithm>
#include <cmath>
#include <ctime>
#include <set>

#include "core/base58.hpp"
#include "core/base64.hpp"
#include "core/crypto.hpp"
#include "dex/clmm_math.hpp"
#include "core/programs.hpp"

namespace arb::state {

std::vector<RawAccount> parse_gpa_response(std::string_view body) {
  std::vector<RawAccount> out;
  simdjson::dom::parser p;
  simdjson::padded_string ps(body);
  simdjson::dom::element root;
  if (p.parse(ps).get(root) != simdjson::SUCCESS) return out;
  simdjson::dom::array arr;
  if (root["result"].get(arr) != simdjson::SUCCESS) return out;
  for (simdjson::dom::element e : arr) {
    RawAccount a;
    std::string_view s;
    if (e["pubkey"].get(s) != simdjson::SUCCESS || !b58_decode32(s, a.key)) continue;
    if (e["account"]["owner"].get(s) != simdjson::SUCCESS || !b58_decode32(s, a.owner)) continue;
    simdjson::dom::array data;
    if (e["account"]["data"].get(data) != simdjson::SUCCESS) continue;
    if (data.at(0).get(s) != simdjson::SUCCESS || !b64_decode(s, a.data)) continue;
    out.push_back(std::move(a));
  }
  return out;
}

int32_t tick_array_start(int32_t tick, uint16_t tick_spacing) {
  int32_t span = TICK_ARRAY_SIZE * int32_t(tick_spacing);
  int32_t q = tick / span;
  if ((tick % span) < 0) --q;  // floor division
  return q * span;
}

Pubkey tick_array_pda(const Pubkey& whirlpool, int32_t start) {
  std::string s = std::to_string(start);
  return pda({std::string_view("tick_array"), whirlpool, std::string_view(s)}, prog::WHIRLPOOL);
}

namespace {

bool is_base(const Pubkey& m, const DiscoveryConfig& c) {
  return std::find(c.base_mints.begin(), c.base_mints.end(), m) != c.base_mints.end();
}

}  // namespace

std::optional<PoolDef> pool_def_from_account(const RawAccount& a, const DiscoveryConfig& cfg) {
  PoolDef d;
  d.address = a.key;
  if (a.owner == prog::RAYDIUM_V4) {
    auto r = decode_raydium_v4(a.data.data(), a.data.size(), a.owner);
    if (!r) return std::nullopt;
    d.kind = DexKind::RaydiumV4;
    d.mint_a = r->coin_mint;
    d.mint_b = r->pc_mint;
    d.vault_a = r->coin_vault;
    d.vault_b = r->pc_vault;
    return d;
  }
  if (a.owner == prog::RAYDIUM_CPMM) {
    auto c = decode_cpmm_pool(a.data.data(), a.data.size(), a.owner);
    if (!c) return std::nullopt;
    d.kind = DexKind::Cpmm;
    d.mint_a = c->token_0_mint;
    d.mint_b = c->token_1_mint;
    d.vault_a = c->token_0_vault;
    d.vault_b = c->token_1_vault;
    d.config = c->amm_config;
    return d;
  }
  if (a.owner == prog::RAYDIUM_CLMM) {
    auto c = decode_clmm_pool(a.data.data(), a.data.size(), a.owner);
    if (!c) return std::nullopt;
    d.kind = DexKind::Clmm;
    d.mint_a = c->token_mint_0;
    d.mint_b = c->token_mint_1;
    d.vault_a = c->token_vault_0;
    d.vault_b = c->token_vault_1;
    d.config = c->amm_config;
    // The current tick's array, then up to three initialized arrays each way along the pool's bitmap.
    const uint16_t sp = c->tick_spacing;
    int32_t cur = dex::clmm::array_start_index(c->tick_current, sp);
    d.tick_arrays.emplace_back(cur, clmm_tick_array_pda(a.key, cur));
    for (bool zfo : {true, false}) {
      int32_t last = cur;
      for (int k = 0; k < 3; ++k) {
        int32_t next = 0;
        if (!dex::clmm::next_initialized_array(c->tick_array_bitmap, last, sp, zfo, next)) break;
        d.tick_arrays.emplace_back(next, clmm_tick_array_pda(a.key, next));
        last = next;
      }
    }
    return d;
  }
  if (a.owner == prog::PUMPSWAP) {
    auto pp = decode_pump_pool(a.data.data(), a.data.size(), a.owner);
    if (!pp) return std::nullopt;
    d.kind = DexKind::PumpSwap;
    d.mint_a = pp->base_mint;
    d.mint_b = pp->quote_mint;
    d.vault_a = pp->pool_base_token_account;
    d.vault_b = pp->pool_quote_token_account;
    d.config = pda({std::string_view("global_config")}, prog::PUMPSWAP);
    d.fee_config = pda({std::string_view("fee_config"), prog::PUMPSWAP}, prog::PUMP_FEES);
    return d;
  }
  if (a.owner == prog::WHIRLPOOL) {
    auto w = decode_whirlpool(a.data.data(), a.data.size(), a.owner);
    if (!w) return std::nullopt;
    d.kind = DexKind::Whirlpool;
    d.mint_a = w->token_mint_a;
    d.mint_b = w->token_mint_b;
    d.vault_a = w->token_vault_a;
    d.vault_b = w->token_vault_b;
    int32_t base = tick_array_start(w->tick_current_index, w->tick_spacing);
    int32_t span = TICK_ARRAY_SIZE * int32_t(w->tick_spacing);
    for (int o = -cfg.tick_arrays_below; o <= cfg.tick_arrays_above; ++o) {
      int32_t start = base + o * span;
      if (start + span <= MIN_TICK_INDEX - span || start > MAX_TICK_INDEX) continue;
      d.tick_arrays.emplace_back(start, tick_array_pda(a.key, start));
    }
    return d;
  }
  return std::nullopt;
}

std::vector<Pubkey> dependent_keys(const std::vector<RawAccount>& pools, const DiscoveryConfig& cfg) {
  std::set<Pubkey> keys;
  for (auto& a : pools) {
    auto d = pool_def_from_account(a, cfg);
    if (!d) continue;
    keys.insert(d->vault_a);
    keys.insert(d->vault_b);
    keys.insert(d->mint_a);
    keys.insert(d->mint_b);
    if (!is_zero(d->config)) keys.insert(d->config);
    if (!is_zero(d->fee_config)) keys.insert(d->fee_config);
  }
  return {keys.begin(), keys.end()};
}

DiscoveryResult discover(const std::vector<RawAccount>& pool_accounts, const AccountLookup& accounts, const DiscoveryConfig& cfg) {
  DiscoveryResult res;
  struct Item {
    PoolCandidate c;
    uint64_t amt_a = 0, amt_b = 0;
    uint8_t dec_a = 0, dec_b = 0;
  };
  std::vector<Item> items;
  auto reject = [&](PoolCandidate c, const char* why) {
    c.reject = why;
    res.reject_counts[why]++;
    res.rejected.push_back(std::move(c));
  };
  std::map<Pubkey, risk::SafetyVerdict> mint_verdicts;
  for (auto& a : pool_accounts) {
    auto d = pool_def_from_account(a, cfg);
    if (!d) {
      res.reject_counts["decode"]++;
      continue;
    }
    PoolCandidate c{*d, 0, ""};
    if (!is_base(d->mint_a, cfg) && !is_base(d->mint_b, cfg)) { reject(c, "no_base_mint"); continue; }
    auto va = accounts.find(d->vault_a), vb = accounts.find(d->vault_b);
    auto ma = accounts.find(d->mint_a), mb = accounts.find(d->mint_b);
    if (va == accounts.end() || vb == accounts.end() || ma == accounts.end() || mb == accounts.end()) {
      reject(c, "missing_accounts");
      continue;
    }
    const Pubkey* dex = dex_program(d->kind);
    if (!dex) { reject(c, "unsupported_dex"); continue; }
    auto own = risk::check_pool_ownership(a.owner, *dex, va->second.owner, vb->second.owner);
    if (!own.ok) { reject(c, own.reason); continue; }
    auto ta = decode_token_account(va->second.data.data(), va->second.data.size(), va->second.owner);
    auto tb = decode_token_account(vb->second.data.data(), vb->second.data.size(), vb->second.owner);
    auto mia = decode_mint(ma->second.data.data(), ma->second.data.size(), ma->second.owner);
    auto mib = decode_mint(mb->second.data.data(), mb->second.data.size(), mb->second.owner);
    if (!ta || !tb || !mia || !mib) { reject(c, "decode_dependent"); continue; }
    if (ta->mint != d->mint_a || tb->mint != d->mint_b) { reject(c, "vault_mint_mismatch"); continue; }
    bool gate_ok = true;
    switch (d->kind) {
      case DexKind::RaydiumV4: {
        // Raydium v4 swaps only through the legacy token program.
        if (va->second.owner != prog::TOKEN || vb->second.owner != prog::TOKEN) { reject(c, "token2022_raydium"); gate_ok = false; break; }
        auto r = decode_raydium_v4(a.data.data(), a.data.size(), a.owner);
        if (!r->swap_permitted()) { reject(c, "pool_status"); gate_ok = false; }
        break;
      }
      case DexKind::Whirlpool: {
        auto w = decode_whirlpool(a.data.data(), a.data.size(), a.owner);
        if (w->adaptive_fee()) { reject(c, "adaptive_fee_unsupported"); gate_ok = false; break; }
        // The legacy `swap` instruction (the one the executor CPIs) takes the SPL token program only.
        if (va->second.owner != prog::TOKEN || vb->second.owner != prog::TOKEN) { reject(c, "token2022_whirlpool"); gate_ok = false; }
        break;
      }
      case DexKind::Cpmm: {
        auto cp = decode_cpmm_pool(a.data.data(), a.data.size(), a.owner);
        auto cfg_it = accounts.find(d->config);
        if (cfg_it == accounts.end() ||
            !decode_cpmm_config(cfg_it->second.data.data(), cfg_it->second.data.size(), cfg_it->second.owner)) {
          reject(c, "cpmm_config");
          gate_ok = false;
          break;
        }
        if (!cp->swap_enabled()) { reject(c, "pool_status"); gate_ok = false; break; }
        if (cp->open_time > uint64_t(std::time(nullptr))) { reject(c, "not_open"); gate_ok = false; break; }
        // The vaults' token programs must be the ones the pool records (the swap passes them).
        if (va->second.owner != cp->token_0_program || vb->second.owner != cp->token_1_program) {
          reject(c, "vault_program");
          gate_ok = false;
        }
        break;
      }
      case DexKind::Clmm: {
        auto cl = decode_clmm_pool(a.data.data(), a.data.size(), a.owner);
        auto cfg_it = accounts.find(d->config);
        if (cfg_it == accounts.end() ||
            !decode_clmm_config(cfg_it->second.data.data(), cfg_it->second.data.size(), cfg_it->second.owner)) {
          reject(c, "clmm_config");
          gate_ok = false;
          break;
        }
        if (!cl->swap_enabled()) { reject(c, "pool_status"); gate_ok = false; break; }
        if (cl->dynamic_fee) { reject(c, "dynamic_fee_unsupported"); gate_ok = false; break; }
        if (cl->liquidity == 0) { reject(c, "zero_liquidity"); gate_ok = false; break; }
        if (cl->open_time >= uint64_t(std::time(nullptr))) { reject(c, "not_open"); gate_ok = false; break; }
        int32_t lo = 0, hi = 0;
        dex::clmm::bitmap_start_range(cl->tick_spacing, lo, hi);
        int32_t st = dex::clmm::array_start_index(cl->tick_current, cl->tick_spacing);
        if (st < lo || st >= hi) { reject(c, "bitmap_extension"); gate_ok = false; }
        break;
      }
      case DexKind::PumpSwap: {
        auto pp = decode_pump_pool(a.data.data(), a.data.size(), a.owner);
        // v2 trades refuse cashback and mayhem pools (errors 6079 / 6080).
        if (pp->is_cashback_coin) { reject(c, "pump_cashback"); gate_ok = false; break; }
        if (pp->is_mayhem_mode) { reject(c, "pump_mayhem"); gate_ok = false; break; }
        auto gc = accounts.find(d->config);
        auto fc = accounts.find(d->fee_config);
        if (gc == accounts.end() || !decode_pump_global_config(gc->second.data.data(), gc->second.data.size(), gc->second.owner) ||
            fc == accounts.end() || !decode_pump_fee_config(fc->second.data.data(), fc->second.data.size(), fc->second.owner)) {
          reject(c, "pump_config");
          gate_ok = false;
        }
        break;
      }
      default:
        reject(c, "unsupported_dex");
        gate_ok = false;
    }
    if (!gate_ok) continue;
    bool safe = true;
    for (auto [key, m] : {std::pair{d->mint_a, &*mia}, std::pair{d->mint_b, &*mib}}) {
      auto it = mint_verdicts.find(key);
      if (it == mint_verdicts.end()) it = mint_verdicts.emplace(key, risk::check_mint(key, *m, cfg.safety)).first;
      if (!it->second.ok) {
        reject(c, it->second.reason);
        safe = false;
        break;
      }
    }
    if (!safe) continue;
    items.push_back(Item{c, ta->amount, tb->amount, mia->decimals, mib->decimals});
  }

  // SOL price in USDC from the deepest SOL/USDC pool (by USDC depth).
  const Pubkey& sol = mint::WSOL;
  const Pubkey& usdc = mint::USDC;
  uint64_t best_usdc = 0;
  for (auto& it : items) {
    const PoolDef& d = it.c.def;
    if (d.mint_a == sol && d.mint_b == usdc && it.amt_b > best_usdc && it.amt_a > 0) {
      best_usdc = it.amt_b;
      res.sol_price_usdc = (double(it.amt_b) / 1e6) / (double(it.amt_a) / 1e9);
    } else if (d.mint_a == usdc && d.mint_b == sol && it.amt_a > best_usdc && it.amt_b > 0) {
      best_usdc = it.amt_a;
      res.sol_price_usdc = (double(it.amt_a) / 1e6) / (double(it.amt_b) / 1e9);
    }
  }
  // TVL in SOL: twice the base-side vault value (SOL directly; USDC/USDT at the SOL price).
  auto base_value_sol = [&](const Pubkey& m, uint64_t amount) -> double {
    if (m == sol) return double(amount) / 1e9;
    if ((m == usdc || m == mint::USDT) && res.sol_price_usdc > 0) return double(amount) / 1e6 / res.sol_price_usdc;
    return -1;
  };
  for (auto& it : items) {
    double va = base_value_sol(it.c.def.mint_a, it.amt_a), vb = base_value_sol(it.c.def.mint_b, it.amt_b);
    it.c.tvl_sol = 2 * std::max(va, vb);
  }
  // Token graph: drop pools whose non-base mint is in only one pool (it cannot close a cycle).
  std::map<Pubkey, int> degree;
  for (auto& it : items) {
    degree[it.c.def.mint_a]++;
    degree[it.c.def.mint_b]++;
  }
  std::vector<Item> kept;
  for (auto& it : items) {
    const PoolDef& d = it.c.def;
    bool ok_a = is_base(d.mint_a, cfg) || degree[d.mint_a] >= 2;
    bool ok_b = is_base(d.mint_b, cfg) || degree[d.mint_b] >= 2;
    if (!ok_a || !ok_b) { reject(it.c, "single_pool_mint"); continue; }
    if (it.c.tvl_sol < cfg.min_pool_tvl_sol) { reject(it.c, "tvl"); continue; }
    kept.push_back(it);
  }
  std::sort(kept.begin(), kept.end(), [](const Item& a, const Item& b) { return a.c.tvl_sol > b.c.tvl_sol; });
  for (size_t i = 0; i < kept.size(); ++i) {
    if (i >= cfg.max_tracked_pools) { reject(kept[i].c, "max_tracked_pools"); continue; }
    res.tracked.push_back(kept[i].c);
  }
  for (size_t i = 0; i < res.tracked.size(); ++i) {
    res.graph[res.tracked[i].def.mint_a].push_back(i);
    res.graph[res.tracked[i].def.mint_b].push_back(i);
  }
  return res;
}

}  // namespace arb::state
