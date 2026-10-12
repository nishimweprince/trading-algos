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

void run_aux_fast(const Config& cfg, Shared& sh, const Pubkey& wallet, Logger& log);
void run_aux_req(const Config& cfg, Shared& sh, const Pubkey& wallet, Logger& log);

}  // namespace pd
