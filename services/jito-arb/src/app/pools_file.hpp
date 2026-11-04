#pragma once
// data/pools.json: discovery's output, the tracked set the feed subscribes to.
//   {"pools":[{"kind":"raydium_v4","address":..,"mint_a":..,"mint_b":..,"vault_a":..,"vault_b":..,
//              "tvl_sol":..,"tick_arrays":[[start,"pda"],...]}], "reserves":[..]}
#include <string>
#include <vector>

#include "state/state_store.hpp"

namespace arb::app {

struct PoolsFile {
  std::vector<PoolDef> pools;
  std::vector<double> tvl_sol;
  std::vector<Pubkey> reserves;
};

std::string to_json(const PoolsFile& f);
bool from_json(const std::string& body, PoolsFile& out, std::string* err);

}  // namespace arb::app
