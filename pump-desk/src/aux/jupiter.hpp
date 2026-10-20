#pragma once
// Jupiter Swap API (api.jup.ag/swap/v1, x-api-key): the exit of last resort.
// When the PumpSwap sell is exhausted (exits.max_exit_attempts), aux-req asks
// Jupiter for a route to SOL over every pool it indexes, signs the v0
// transaction Jupiter builds and sends it through the RPC. Never on the
// trading path: a quote and a swap build are two HTTP round trips.
#include <cstdint>
#include <string>
#include <vector>

#include "aux/rpc.hpp"
#include "core/config.hpp"
#include "core/types.hpp"
#include "sol/crypto.hpp"

namespace pd {

struct JupiterSwap {
  std::string tx_b64;      // unsigned v0 transaction, as Jupiter returns it
  std::string sim_error;   // Jupiter's own simulation failed: don't send
  uint64_t out_lamports = 0;
  uint64_t min_out_lamports = 0;
  std::string route;       // AMM labels, for the log
};

class Jupiter {
 public:
  explicit Jupiter(const Config& cfg);

  /** Quote `amount` raw tokens of `mint` -> SOL and build the swap tx for `user`. Throws on HTTP/API errors. */
  JupiterSwap sell_to_sol(const Pubkey& mint, uint64_t amount, const Pubkey& user);

 private:
  std::string get(const std::string& path);
  std::string post(const std::string& path, const std::string& body);

  const Config& cfg_;
  Rpc http_;
  std::string base_path_, headers_;
};

/**
 * Sign a serialized one-signer transaction in place. Refuses (false, `err`) unless it
 * has exactly one signature slot and its fee payer, the first account key, is `kp`.
 */
bool sign_single_signer_tx(std::vector<uint8_t>& tx, const Keypair& kp, std::string* err);

}  // namespace pd
