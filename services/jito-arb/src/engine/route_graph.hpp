#pragma once
// Route graph and cycle index (spec §6.1). Built at discovery time from the tracked pools:
// every 2-hop (base -> X -> base) and 3-hop (base -> X -> Y -> base) cycle, capped per pool
// at max_cycles_per_pool, keeping the cycles whose shallowest pool is deepest.
#include <cstdint>
#include <span>
#include <vector>

#include "state/state_store.hpp"

namespace arb::engine {

constexpr int MAX_LEGS = 3;

struct Leg {
  uint32_t pool = 0;
  bool a_to_b = true;
  Pubkey mint_in{}, mint_out{};
};

struct Cycle {
  uint32_t id = 0;
  uint8_t n = 0;
  Leg legs[MAX_LEGS];
  Pubkey base{};
  uint16_t route_class = 0;  // (hops << 8) | bitmask of DexKind values on the route
  double score = 0;          // shallowest leg's TVL (discovery input); ordering only
};

/** route_class = hops in the high byte, one bit per DexKind in the low byte. */
uint16_t route_class_of(uint8_t hops, std::span<const DexKind> kinds);

struct GraphConfig {
  std::vector<Pubkey> base_mints;
  int max_hops = 3;
  int max_cycles_per_pool = 64;
  size_t max_cycles = 200'000;  // global cap on enumeration (a guard, not a spec value)
};

class CycleIndex {
 public:
  /** `tvl[i]` scores pool i (discovery's TVL in SOL); a missing entry scores 0. */
  void build(std::span<const PoolDef> pools, std::span<const double> tvl, const GraphConfig& cfg);
  const std::vector<uint32_t>& cycles_of(uint32_t pool) const;
  const Cycle& cycle(uint32_t id) const { return cycles_[id]; }
  size_t size() const { return cycles_.size(); }
  const std::vector<Cycle>& all() const { return cycles_; }

 private:
  std::vector<Cycle> cycles_;
  std::vector<std::vector<uint32_t>> by_pool_;
  static inline const std::vector<uint32_t> empty_{};
};

}  // namespace arb::engine
