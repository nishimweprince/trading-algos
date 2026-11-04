#include "engine/route_graph.hpp"

#include <algorithm>
#include <map>

namespace arb::engine {

uint16_t route_class_of(uint8_t hops, std::span<const DexKind> kinds) {
  uint16_t mask = 0;
  for (DexKind k : kinds) mask |= uint16_t(1u << uint8_t(k));
  return uint16_t((uint16_t(hops) << 8) | (mask & 0xFF));
}

namespace {

struct Edge {
  uint32_t pool;
  Pubkey other;
  bool a_to_b;  // direction when entering the pool from the keyed mint
};

}  // namespace

void CycleIndex::build(std::span<const PoolDef> pools, std::span<const double> tvl, const GraphConfig& cfg) {
  cycles_.clear();
  by_pool_.assign(pools.size(), {});
  std::map<Pubkey, std::vector<Edge>> adj;
  for (uint32_t i = 0; i < pools.size(); ++i) {
    const PoolDef& p = pools[i];
    if (p.mint_a == p.mint_b) continue;
    adj[p.mint_a].push_back({i, p.mint_b, true});
    adj[p.mint_b].push_back({i, p.mint_a, false});
  }
  auto score = [&](uint32_t pool) { return pool < tvl.size() ? tvl[pool] : 0.0; };
  std::vector<Cycle> all;
  auto emit = [&](const Pubkey& base, std::initializer_list<std::pair<const Edge*, Pubkey>> legs) {
    if (all.size() >= cfg.max_cycles) return;
    Cycle c;
    c.base = base;
    DexKind kinds[MAX_LEGS];
    double s = 1e300;
    for (auto& [e, from] : legs) {
      Leg& l = c.legs[c.n];
      l.pool = e->pool;
      l.a_to_b = e->a_to_b;
      l.mint_in = from;
      l.mint_out = e->other;
      kinds[c.n] = pools[e->pool].kind;
      s = std::min(s, score(e->pool));
      ++c.n;
    }
    c.route_class = route_class_of(c.n, std::span<const DexKind>(kinds, c.n));
    c.score = s;
    all.push_back(c);
  };
  for (const Pubkey& base : cfg.base_mints) {
    auto bi = adj.find(base);
    if (bi == adj.end()) continue;
    for (const Edge& e1 : bi->second) {
      const Pubkey& x = e1.other;
      auto xi = adj.find(x);
      if (xi == adj.end()) continue;
      for (const Edge& e2 : xi->second) {
        if (e2.pool == e1.pool) continue;
        if (e2.other == base) {
          if (cfg.max_hops >= 2) emit(base, {{&e1, base}, {&e2, x}});
          continue;
        }
        if (cfg.max_hops < 3) continue;
        const Pubkey& y = e2.other;
        auto yi = adj.find(y);
        if (yi == adj.end()) continue;
        for (const Edge& e3 : yi->second) {
          if (e3.other != base || e3.pool == e1.pool || e3.pool == e2.pool) continue;
          emit(base, {{&e1, base}, {&e2, x}, {&e3, y}});
        }
      }
    }
  }
  // Deepest first. The cap bounds what one pool's update evaluates: each pool's list holds its
  // max_cycles_per_pool deepest cycles. A cycle is kept if at least one of its pools lists it, so a busy hub
  // pool (SOL/USDC) with a full list no longer blocks cycles that its quieter legs still evaluate.
  std::stable_sort(all.begin(), all.end(), [](const Cycle& a, const Cycle& b) { return a.score > b.score; });
  std::vector<int> count(pools.size(), 0);
  for (Cycle& c : all) {
    bool listed = false;
    for (int i = 0; i < c.n; ++i) listed |= count[c.legs[i].pool] < cfg.max_cycles_per_pool;
    if (!listed) continue;
    c.id = uint32_t(cycles_.size());
    for (int i = 0; i < c.n; ++i) {
      uint32_t p = c.legs[i].pool;
      if (count[p] >= cfg.max_cycles_per_pool) continue;
      // A pool used twice on one route (impossible by construction) would be indexed once.
      if (by_pool_[p].empty() || by_pool_[p].back() != c.id) by_pool_[p].push_back(c.id);
      ++count[p];
    }
    cycles_.push_back(c);
  }
}

const std::vector<uint32_t>& CycleIndex::cycles_of(uint32_t pool) const {
  return pool < by_pool_.size() ? by_pool_[pool] : empty_;
}

}  // namespace arb::engine
