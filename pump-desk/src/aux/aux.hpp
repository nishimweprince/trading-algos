#pragma once
// Aux threads (unpinned, may block on RPC):
//   AuxFast — blockhash, PumpSwap configs, priority fee, wallet balance, KILL file
//   AuxReq  — curve -> mint resolution, mint reads, unconfirmed-order resolution
#include <string>

#include "core/config.hpp"
#include "logic/shared.hpp"
#include "sol/crypto.hpp"

namespace pd {

class Logger;

/** One refresh of the PumpSwap configs into `sh.amm`. Throws on RPC failure. */
void refresh_amm_config(class Rpc& rpc, Shared& sh);

/** data/rivals.json: the wallets to guard against and, per class, the CU price that beats them. */
struct RivalsFile {
  RivalSet set;
  uint64_t beat_cu_price[N_LEADER_CLASSES] = {0};
};
/** {"guard": ["wallet", ...], "beat_cu_price": {"agave": n, "bam": n, "firedancer": n}}; `self` is never a rival. Throws on bad JSON. */
RivalsFile parse_rivals(std::string_view json, const Pubkey& self, size_t max_rivals);

/** Per-class fee/CU/route from the Helius estimate (0 = none yet), the config and the rivals' prices. */
RoutePolicy make_route_policy(const Config& cfg, uint64_t estimate, const uint64_t beat_cu_price[N_LEADER_CLASSES]);

void run_aux_fast(const Config& cfg, Shared& sh, const Pubkey& wallet, Logger& log);
void run_aux_req(const Config& cfg, Shared& sh, const Pubkey& wallet, Logger& log);

}  // namespace pd
