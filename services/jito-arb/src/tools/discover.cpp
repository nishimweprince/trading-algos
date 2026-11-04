// Pool discovery (spec §4): getProgramAccounts per DEX and base mint, then the token graph,
// TVL filter and safety gate. Writes the tracked set and prints the subscription diff.
//
//   discover --config FILE [--out data/pools.json] [--previous data/pools.json]
//
// Needs an RPC that serves filtered getProgramAccounts at volume (Helius on the VM); the
// laptop tests run the same code on recorded responses (tests/fixtures/gpa_*.json).
#include <chrono>
#include <cstdio>
#include <fstream>
#include <set>
#include <sstream>
#include <thread>

#include "app/pools_file.hpp"
#include "core/base58.hpp"
#include "core/config.hpp"
#include "core/crypto.hpp"
#include "core/programs.hpp"
#include "core/rpc.hpp"
#include "feed/subscription.hpp"
#include "state/discovery.hpp"

using namespace arb;

int main(int argc, char** argv) {
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
  for (const Pubkey& p : {prog::RAYDIUM_V4, prog::WHIRLPOOL, prog::KLEND}) {
    auto pi = rpc.program_info(p);
    if (!pi.found || !pi.executable) {
      std::fprintf(stderr, "program %s is not an executable account\n", b58(p).c_str());
      return 1;
    }
  }
  std::vector<state::RawAccount> pools;
  struct Q { Pubkey program; uint64_t size; uint64_t offset; };
  std::vector<Q> queries;
  if (cfg.dex.raydium_v4_enabled) {
    queries.push_back({prog::RAYDIUM_V4, state::RAYDIUM_V4_LEN, 400});  // coin mint
    queries.push_back({prog::RAYDIUM_V4, state::RAYDIUM_V4_LEN, 432});  // pc mint
  }
  if (cfg.dex.whirlpool_enabled) {
    queries.push_back({prog::WHIRLPOOL, state::WHIRLPOOL_LEN, 101});  // token_mint_a
    queries.push_back({prog::WHIRLPOOL, state::WHIRLPOOL_LEN, 181});  // token_mint_b
  }
  std::set<Pubkey> seen;
  for (auto& q : queries) {
    for (auto& base : dc.base_mints) {
      std::string body = rpc.program_accounts_raw(q.program, q.size, {{q.offset, b58(base)}});
      for (auto& a : state::parse_gpa_response(body))
        if (seen.insert(a.key).second) pools.push_back(std::move(a));
      std::this_thread::sleep_for(std::chrono::milliseconds(250));
    }
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
  std::ofstream(out) << app::to_json(pf);
  std::fprintf(stderr, "tracked %zu pools (SOL = %.2f USDC); rejected:", res.tracked.size(), res.sol_price_usdc);
  for (auto& [why, n] : res.reject_counts) std::fprintf(stderr, " %s=%zu", why.c_str(), n);
  std::fprintf(stderr, "\n");
  if (!previous.empty()) {
    std::ifstream f(previous);
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
