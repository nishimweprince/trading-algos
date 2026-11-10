#include "app/live_plan.hpp"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <map>
#include <set>

#include "core/types.hpp"
#include "state/state_store.hpp"

namespace arb::app {

std::vector<PoolScore> rank_pools(const std::vector<obs::DecisionRecord>& records, int64_t min_net_lamports, uint64_t max_lag_slots) {
  using Sig = std::array<uint8_t, 64>;
  auto sig_of = [](const obs::DecisionRecord& r) {
    Sig s;
    std::memcpy(s.data(), r.signature, 64);
    return s;
  };
  std::map<Sig, const obs::DecisionRecord*> cands;
  for (const auto& r : records)
    if (r.kind == uint8_t(obs::RecordKind::Candidate) && r.action == uint8_t(obs::Action::DryRun)) cands[sig_of(r)] = &r;
  std::map<Pubkey, PoolScore> score;
  for (const auto& o : records) {
    if (o.kind != uint8_t(obs::RecordKind::Outcome) || o.outcome != uint8_t(obs::Outcome::SimulatedOk)) continue;
    if (o.net_lamports < min_net_lamports) continue;
    auto it = cands.find(sig_of(o));
    if (it == cands.end()) continue;
    const obs::DecisionRecord& c = *it->second;
    if (!o.slot || o.slot < c.slot || o.slot - c.slot > max_lag_slots) continue;
    for (int i = 0; i < c.hops && i < 3; ++i) {
      Pubkey k = key_from(c.pools[i]);
      PoolScore& s = score[k];
      s.pool = k;
      ++s.passes;
      s.net_lamports += o.net_lamports;
    }
  }
  std::vector<PoolScore> out;
  for (auto& [k, s] : score) out.push_back(s);
  std::sort(out.begin(), out.end(), [](const PoolScore& a, const PoolScore& b) {
    return a.passes != b.passes ? a.passes > b.passes : a.net_lamports != b.net_lamports ? a.net_lamports > b.net_lamports : a.pool < b.pool;
  });
  return out;
}

LivePlan size_plan(const PoolsFile& pools, const std::vector<PoolScore>& ranking, size_t max_pools,
                   const std::vector<Pubkey>& base_mints, size_t extra_keys) {
  LivePlan p;
  std::map<Pubkey, const PoolDef*> by_address;
  for (const PoolDef& d : pools.pools) by_address[d.address] = &d;
  StateStore store;
  std::set<Pubkey> mints;
  for (const PoolScore& s : ranking) {
    if (p.armed.size() >= max_pools) break;
    auto it = by_address.find(s.pool);
    if (it == by_address.end()) {
      ++p.dropped;
      continue;
    }
    store.add_pool(*it->second);
    p.armed.push_back(s.pool);
    for (const Pubkey& m : {it->second->mint_a, it->second->mint_b})
      if (std::find(base_mints.begin(), base_mints.end(), m) == base_mints.end()) mints.insert(m);
  }
  if (p.armed.empty()) return p;
  p.table_keys = store.subscribed_keys().size() + extra_keys;
  p.tables = (p.table_keys + ALT_ADDRESSES - 1) / ALT_ADDRESSES;
  for (size_t left = p.table_keys; left;) {
    size_t n = std::min(left, ALT_ADDRESSES);
    p.table_rent_lamports += rent_exempt_lamports(ALT_META_BYTES + 32 * n);
    left -= n;
  }
  p.token_accounts = mints.size();
  p.token_rent_lamports = p.token_accounts * rent_exempt_lamports(TOKEN_ACCOUNT_BYTES);
  return p;
}

std::string plan_report(const LivePlan& p, size_t ranked, size_t max_pools) {
  char b[1024];
  std::snprintf(b, sizeof b,
                "pools on a guard-passing fresh dry-run route: %zu\n"
                "armed (cap %zu): %zu%s\n"
                "lookup tables: %zu keys in %zu tables, rent %.4f SOL\n"
                "token accounts: %zu intermediate mints, rent at least %.4f SOL (Token-2022 accounts cost more)\n"
                "total rent to lock: %.4f SOL (returned when the tables and accounts are closed)\n",
                ranked, max_pools, p.armed.size(), p.dropped ? " (some ranked pools are no longer tracked)" : "",
                p.table_keys, p.tables, double(p.table_rent_lamports) / 1e9, p.token_accounts,
                double(p.token_rent_lamports) / 1e9, double(p.total_rent_lamports()) / 1e9);
  return b;
}

}  // namespace arb::app
