// Pool discovery (spec §4): getProgramAccounts per DEX and base mint, then the token graph,
// TVL filter and safety gate. Writes the tracked set and prints the subscription diff.
//
//   discover --config FILE [--out data/pools.json] [--previous data/pools.json]
//
// Needs an RPC that serves filtered getProgramAccounts at volume (Helius on the VM); the
// laptop tests run the same code on recorded responses (tests/fixtures/gpa_*.json).
#include <simdjson.h>

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <map>
#include <set>
#include <sstream>
#include <thread>

#include "app/pools_file.hpp"
#include "core/base58.hpp"
#include "core/base64.hpp"
#include "core/config.hpp"
#include "core/crypto.hpp"
#include "core/programs.hpp"
#include "core/rpc.hpp"
#include "feed/subscription.hpp"
#include "state/discovery.hpp"
#include "tx/dex_ix.hpp"

using namespace arb;

namespace {
int run(int argc, char** argv) {
  std::string cfg_path, out = "data/pools.json", previous;
  for (int i = 1; i + 1 < argc; i += 2) {
    std::string a = argv[i];
    if (a == "--config") cfg_path = argv[i + 1];
    else if (a == "--out") out = argv[i + 1];
    else if (a == "--previous") previous = argv[i + 1];
  }
  crypto_init();
  Config cfg = load_config(cfg_path);
  Rpc rpc(resolve_rpc_url(cfg), 60000);
  state::DiscoveryConfig dc;
  for (auto& m : cfg.engine.base_mints) dc.base_mints.push_back(parse_pubkey(m));
  for (auto& m : cfg.risk.freeze_allowlist) dc.safety.freeze_allowlist.push_back(parse_pubkey(m));
  dc.safety.transfer_fee_max_bps = cfg.risk.transfer_fee_max_bps;
  dc.min_pool_tvl_sol = cfg.discovery.min_pool_tvl_sol;
  dc.max_tracked_pools = size_t(cfg.discovery.max_tracked_pools);

  // Confirm the program IDs before anything else.
  for (const Pubkey& p : {prog::RAYDIUM_V4, prog::WHIRLPOOL, prog::RAYDIUM_CPMM, prog::PUMPSWAP, prog::KLEND}) {
    auto pi = rpc.program_info(p);
    if (!pi.found || !pi.executable) {
      std::fprintf(stderr, "program %s is not an executable account\n", b58(p).c_str());
      return 1;
    }
  }
  std::vector<state::RawAccount> pools;
  std::set<Pubkey> seen;
  // Constant-product pools with one authority PDA holding every vault (Raydium v4, CPMM). Hundreds of thousands
  // pair wSOL (about 690k Raydium v4 in Nov 2026, ~700 MB): their full accounts do not come back in one
  // getProgramAccounts. Instead: (1) the 8-byte amount of every vault the authority holds for each base mint,
  // (2) each pool's vault/mint slice, (3) full accounts only for pools whose base-side vault could clear
  // min_pool_tvl_sol (TVL >= 2 x base side), deepest first, up to `cap`.
  //   slice: [vault_0, vault_1] at slice offsets 0 and 32; mint_off_0/1 are the account offsets of mint 0/1.
  auto vault_floor_fetch = [&](const char* name, const Pubkey& program, const Pubkey& authority, size_t data_size, uint64_t slice_off,
                               uint64_t slice_len, uint64_t mint_off_0, uint64_t mint_off_1, int cap_cfg) {
    std::map<Pubkey, uint64_t> vault_amount;
    std::map<Pubkey, uint64_t> threshold;  // base mint -> minimum base-side vault amount
    for (auto& base : dc.base_mints) {
      std::string body = rpc.call("getTokenAccountsByOwner",
                                  "[\"" + b58(authority) + "\",{\"mint\":\"" + b58(base) +
                                      "\"},{\"encoding\":\"base64\",\"commitment\":\"confirmed\",\"dataSlice\":{\"offset\":64,\"length\":8}}]");
      simdjson::dom::parser p;
      simdjson::padded_string ps(body);
      for (auto e : simdjson::dom::array(p.parse(ps)["result"]["value"])) {
        Pubkey k;
        std::vector<uint8_t> amt;
        if (!b58_decode32(std::string_view(e["pubkey"]), k)) continue;
        b64_decode(std::string_view(simdjson::dom::array(e["account"]["data"]).at(0)), amt);
        if (amt.size() != 8) continue;
        uint64_t v;
        std::memcpy(&v, amt.data(), 8);
        vault_amount[k] = v;
      }
      // wSOL in lamports; other bases (USDC, USDT: 6 decimals) at a deliberately low 50 per SOL, so the exact
      // TVL filter in discover() makes the final cut.
      double half_sol = dc.min_pool_tvl_sol / 2;
      threshold[base] = base == mint::WSOL ? uint64_t(half_sol * 1e9) : uint64_t(half_sol * 50 * 1e6);
      std::this_thread::sleep_for(std::chrono::milliseconds(250));
    }
    std::vector<std::pair<uint64_t, Pubkey>> cand;  // (base-side vault amount, pool)
    for (auto& base : dc.base_mints) {
      for (uint64_t off : {mint_off_0, mint_off_1}) {
        std::string body = rpc.call("getProgramAccounts",
                                    "[\"" + b58(program) + "\",{\"encoding\":\"base64\",\"commitment\":\"confirmed\","
                                    "\"dataSlice\":{\"offset\":" + std::to_string(slice_off) + ",\"length\":" + std::to_string(slice_len) +
                                        "},\"filters\":[{\"dataSize\":" + std::to_string(data_size) + "},{\"memcmp\":{\"offset\":" +
                                        std::to_string(off) + ",\"bytes\":\"" + b58(base) + "\"}}]}]");
        simdjson::dom::parser p;
        simdjson::padded_string ps(body);
        for (auto e : simdjson::dom::array(p.parse(ps)["result"])) {
          Pubkey k;
          std::vector<uint8_t> d;
          if (!b58_decode32(std::string_view(e["pubkey"]), k)) continue;
          b64_decode(std::string_view(simdjson::dom::array(e["account"]["data"]).at(0)), d);
          if (d.size() != slice_len) continue;
          bool base_is_0 = off == mint_off_0;
          auto it = vault_amount.find(key_from(d.data() + (base_is_0 ? 0 : 32)));
          if (it == vault_amount.end() || it->second < threshold[base]) continue;
          cand.emplace_back(it->second, k);
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
      }
    }
    std::sort(cand.begin(), cand.end(), [](auto& a, auto& b) { return a.first > b.first; });
    size_t cap = size_t(std::max(1, cap_cfg));
    std::vector<Pubkey> keys;
    for (auto& [amt, k] : cand)
      if (keys.size() < cap && seen.insert(k).second) keys.push_back(k);
    std::fprintf(stderr, "%s: %zu base vaults read, %zu pools above the vault floor, fetching %zu\n", name, vault_amount.size(),
                 cand.size(), keys.size());
    for (size_t i = 0; i < keys.size(); i += 100) {
      std::vector<Pubkey> batch(keys.begin() + long(i), keys.begin() + long(std::min(keys.size(), i + 100)));
      auto accs = rpc.multiple_accounts(batch);
      for (size_t j = 0; j < batch.size() && j < accs.size(); ++j)
        if (accs[j]) pools.push_back(state::RawAccount{batch[j], accs[j]->owner, accs[j]->data});
      std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
  };
  if (cfg.dex.raydium_v4_enabled)
    // slice 336..463: coin_vault, pc_vault, coin_mint (400), pc_mint (432)
    vault_floor_fetch("raydium_v4", prog::RAYDIUM_V4, tx::dexix::raydium_authority(), state::RAYDIUM_V4_LEN, 336, 128, 400, 432,
                      cfg.discovery.raydium_fetch_cap);
  if (cfg.dex.cpmm_enabled || cfg.discovery.track_cpmm)
    // slice 72..231: token_0_vault, token_1_vault, lp_mint, token_0_mint (168), token_1_mint (200)
    vault_floor_fetch("cpmm", prog::RAYDIUM_CPMM, tx::dexix::cpmm_authority(), state::CPMM_POOL_LEN, 72, 160, 168, 200,
                      cfg.discovery.cpmm_fetch_cap);
  if (cfg.dex.whirlpool_enabled || cfg.discovery.track_whirlpool) {
    // ~143k Whirlpools pair wSOL or USDC (157 MB, Nov 2026). Keep live, static-fee pools only: zero
    // liquidity cannot trade, and adaptive-fee pools are refused by the quoter.
    size_t seen_wp = 0, kept_wp = 0;
    for (uint64_t off : {uint64_t(101), uint64_t(181)}) {  // token_mint_a, token_mint_b
      for (auto& base : dc.base_mints) {
        std::string body = rpc.program_accounts_raw(prog::WHIRLPOOL, state::WHIRLPOOL_LEN, {{off, b58(base)}});
        for (auto& a : state::parse_gpa_response(body)) {
          ++seen_wp;
          auto w = state::decode_whirlpool(a.data.data(), a.data.size(), a.owner);
          if (!w || w->liquidity == 0 || w->adaptive_fee()) continue;
          if (seen.insert(a.key).second) {
            pools.push_back(std::move(a));
            ++kept_wp;
          }
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
      }
    }
    std::fprintf(stderr, "whirlpool: %zu read, %zu live static-fee pools%s\n", seen_wp, kept_wp,
                 cfg.dex.whirlpool_enabled ? "" : " (tracked only: quoting disabled)");
  }
  if (cfg.dex.pumpswap_enabled || cfg.discovery.track_pumpswap) {
    // Canonical PumpSwap pools (created by pump migration): PDA ["pool", u16 0, pool_authority(mint), mint, wSOL]
    // with pool_authority = pump PDA ["pool-authority", mint]. Derived for every token the other DEXes found
    // (a cycle needs a second pool for the token anyway), then read; those that exist join the candidates.
    std::set<Pubkey> bases(dc.base_mints.begin(), dc.base_mints.end());
    std::set<Pubkey> tokens;
    for (auto& a : pools) {
      auto d = state::pool_def_from_account(a, dc);
      if (!d) continue;
      for (const Pubkey& m : {d->mint_a, d->mint_b})
        if (!bases.count(m)) tokens.insert(m);
    }
    std::vector<Pubkey> keys;
    const uint8_t index0[2] = {0, 0};
    for (const Pubkey& m : tokens) {
      Pubkey authority = pda({std::string_view("pool-authority"), m}, prog::PUMP);
      Pubkey pool = pda({std::string_view("pool"), Seed(index0, 2), authority, m, mint::WSOL}, prog::PUMPSWAP);
      if (seen.insert(pool).second) keys.push_back(pool);
    }
    size_t found = 0;
    for (size_t i = 0; i < keys.size(); i += 100) {
      std::vector<Pubkey> batch(keys.begin() + long(i), keys.begin() + long(std::min(keys.size(), i + 100)));
      auto accs = rpc.multiple_accounts(batch);
      for (size_t j = 0; j < batch.size() && j < accs.size(); ++j)
        if (accs[j] && accs[j]->owner == prog::PUMPSWAP) {
          pools.push_back(state::RawAccount{batch[j], accs[j]->owner, accs[j]->data});
          ++found;
        }
      std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    std::fprintf(stderr, "pumpswap: %zu canonical pools derived from %zu tokens, %zu exist%s\n", keys.size(), tokens.size(), found,
                 cfg.dex.pumpswap_enabled ? "" : " (tracked only: quoting disabled)");
  }
  // A pool can sit on a cycle only if its non-base token has a second pool: drop the rest before the
  // vault/mint lookups (discover() applies the same rule exactly after the TVL filter).
  {
    std::set<Pubkey> bases(dc.base_mints.begin(), dc.base_mints.end());
    auto mints_of = [](const state::RawAccount& a) -> std::pair<Pubkey, Pubkey> {
      if (auto r = state::decode_raydium_v4(a.data.data(), a.data.size(), a.owner)) return {r->coin_mint, r->pc_mint};
      if (auto w = state::decode_whirlpool(a.data.data(), a.data.size(), a.owner)) return {w->token_mint_a, w->token_mint_b};
      if (auto c = state::decode_cpmm_pool(a.data.data(), a.data.size(), a.owner)) return {c->token_0_mint, c->token_1_mint};
      if (auto pp = state::decode_pump_pool(a.data.data(), a.data.size(), a.owner)) return {pp->base_mint, pp->quote_mint};
      return {Pubkey{}, Pubkey{}};
    };
    std::map<Pubkey, int> count;
    for (auto& a : pools) {
      auto [x, y] = mints_of(a);
      ++count[x];
      ++count[y];
    }
    std::vector<state::RawAccount> kept;
    for (auto& a : pools) {
      auto [x, y] = mints_of(a);
      bool ok = (bases.count(x) || count[x] >= 2) && (bases.count(y) || count[y] >= 2);
      if (ok) kept.push_back(std::move(a));
    }
    std::fprintf(stderr, "cycle prefilter: %zu of %zu pools have a token in a second pool\n", kept.size(), pools.size());
    pools = std::move(kept);
  }
  std::fprintf(stderr, "candidate pools: %zu\n", pools.size());
  auto keys = state::dependent_keys(pools, dc);
  state::AccountLookup lookup;
  for (size_t i = 0; i < keys.size(); i += 100) {
    std::vector<Pubkey> batch(keys.begin() + long(i), keys.begin() + long(std::min(keys.size(), i + 100)));
    auto accs = rpc.multiple_accounts(batch);
    for (size_t j = 0; j < batch.size() && j < accs.size(); ++j)
      if (accs[j]) lookup[batch[j]] = state::RawAccount{batch[j], accs[j]->owner, accs[j]->data};
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
  }
  auto res = state::discover(pools, lookup, dc);
  app::PoolsFile pf;
  for (auto& c : res.tracked) {
    pf.pools.push_back(c.def);
    pf.tvl_sol.push_back(c.tvl_sol);
  }
  for (auto& r : cfg.flash.reserves) pf.reserves.push_back(parse_pubkey(r));
  std::string prev_body;  // read before `out` is overwritten (it is usually the same file)
  if (!previous.empty()) {
    std::ifstream pf_in(previous);
    std::stringstream pss;
    pss << pf_in.rdbuf();
    prev_body = pss.str();
  }
  std::ofstream(out) << app::to_json(pf);
  std::fprintf(stderr, "tracked %zu pools (SOL = %.2f USDC); rejected:", res.tracked.size(), res.sol_price_usdc);
  for (auto& [why, n] : res.reject_counts) std::fprintf(stderr, " %s=%zu", why.c_str(), n);
  std::fprintf(stderr, "\n");
  if (!previous.empty() && !prev_body.empty()) {
    std::istringstream f(prev_body);
    std::stringstream ss;
    ss << f.rdbuf();
    app::PoolsFile old;
    std::string err;
    if (app::from_json(ss.str(), old, &err)) {
      std::vector<Pubkey> a, b;
      for (auto& p : old.pools) a.push_back(p.address);
      for (auto& p : pf.pools) b.push_back(p.address);
      auto d = feed::diff_keys(a, b);
      std::fprintf(stderr, "subscription diff: +%zu -%zu pools\n", d.added.size(), d.removed.size());
    }
  }
  return 0;
}
}  // namespace

int main(int argc, char** argv) {
  try {
    return run(argc, argv);
  } catch (const std::exception& e) {
    std::fprintf(stderr, "discover failed: %s\n", e.what());
    return 1;
  }
}
