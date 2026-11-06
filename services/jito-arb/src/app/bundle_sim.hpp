#pragma once
// Read-only simulation of dry-run bundles (spec §9): did the route the bot signed actually work on chain?
//
// The signed bundle itself cannot run on mainnet: the executor program is not deployed, the lookup tables are
// virtual and the signer is an unfunded test key. So the route's swap legs are simulated directly, the way
// harness/sim_parity.cpp proves the quoters: a public holder of the base token (its canonical ATA holds at
// least the trade size) is the fee payer and swap authority, every leg is a direct DEX swap with the quoted
// input, and simulateTransaction (sigVerify:false, replaceRecentBlockhash:true, processed) returns the base
// account after the route. Its change plus the amount in is the simulated final out; the flash fee, exact
// and proven by sim-check (a), is subtracted in code. Nothing is signed and nothing is sent.
//
// A background thread does the RPC work (Rpc is blocking: never on the engine path). Results come back as
// Outcome decision records keyed by the bundle's signature, drained by the thread that owns the decision log.
#include <array>
#include <atomic>
#include <condition_variable>
#include <deque>
#include <map>
#include <memory>
#include <mutex>
#include <span>
#include <string>
#include <thread>
#include <vector>

#include "core/config.hpp"
#include "core/error.hpp"
#include "engine/dispatcher.hpp"
#include "obs/decision_log.hpp"
#include "state/pool_state.hpp"
#include "tx/message_v0.hpp"

namespace arb {
class Rpc;
}

namespace arb::app {

struct SimJob {
  engine::Opportunity o;
  engine::Cycle c;
  std::array<std::shared_ptr<const PoolState>, engine::MAX_LEGS> legs{};
  obs::DecisionRecord cand;  // the dry-run candidate record (signature, tip, fee, gross)
};

/** A public wallet used as payer and swap authority: its canonical ATA of the base token holds `balance`. */
struct SimHolder {
  Pubkey owner{}, token_account{};
  uint64_t balance = 0;
};

struct SimLayout {
  uint8_t first_swap = 0;          // instruction index of leg 1 (after the ATA creates)
  Pubkey base_account{};           // the holder's base-token ATA (watched)
  std::vector<Pubkey> intermediates;  // the holder's ATAs for the route's intermediate tokens, in leg order
};

/** The holder's ATAs the route touches: base first, then each intermediate token in leg order. */
SimLayout sim_accounts(const engine::Cycle& c, std::span<const std::shared_ptr<const PoolState>> legs, const Pubkey& holder);

/**
 * [create ATA (idempotent) for each intermediate in `need_ata`][one direct swap per leg]. Leg 1 takes o.x, each
 * later leg the previous leg's quoted output; min_out is 0 (the outcome is measured, not guarded).
 */
Result<SimLayout> build_sim_ixs(tx::IxList& l, const engine::Cycle& c, std::span<const std::shared_ptr<const PoolState>> legs,
                                const engine::Opportunity& o, const Pubkey& holder, const std::vector<bool>& need_ata);

/** The piece that failed, from simulateTransaction's err JSON ({"InstructionError":[i, ...]}). */
obs::SimFail classify_sim_error(const std::string& err_json, uint8_t first_swap, int n_legs);

/**
 * The Outcome record for a candidate: simulated final out and profit (sim final out - x - flash fee), gross and
 * net in lamports at the candidate's own base-to-lamports rate, units consumed and the failing piece.
 */
obs::DecisionRecord sim_outcome(const SimJob& j, bool ok, obs::SimFail why, uint64_t sim_slot, uint64_t sim_final_out,
                                uint64_t units, int64_t wall_ms);

/** Static v0 message, zero signatures and blockhash (the RPC replaces it), base64. Empty when over 1,232 bytes. */
std::string unsigned_tx_b64(const Pubkey& payer, const tx::IxList& ixs);

class BundleSim {
 public:
  struct Counters {
    std::atomic<uint64_t> submitted{0}, dropped{0}, ok{0}, failed{0}, profitable{0}, no_holder{0}, too_large{0}, rpc_error{0};
  };
  BundleSim(const Config& cfg, std::atomic<bool>& stop);
  ~BundleSim();
  BundleSim(const BundleSim&) = delete;
  BundleSim& operator=(const BundleSim&) = delete;
  void start();
  /** Queue a job (engine thread). False, and counted as dropped, above the rate cap or with a full queue. */
  bool submit(SimJob j);
  /** Finished Outcome records, oldest first (the decision log's thread). */
  std::vector<obs::DecisionRecord> drain();
  const Counters& counters() const { return n_; }

 private:
  void run();
  void process(SimJob& j, Rpc& rpc);
  const SimHolder* holder_for(const Pubkey& mint, uint64_t amount, Rpc& rpc);
  void refresh_holders(const Pubkey& mint, Rpc& rpc);

  const Config& cfg_;
  std::atomic<bool>& stop_;
  std::atomic<bool> quit_{false};
  std::thread thread_;
  std::mutex mu_;
  std::condition_variable cv_;
  std::deque<SimJob> queue_;
  std::vector<obs::DecisionRecord> done_;
  Counters n_;
  double tokens_ = 0;      // rate cap: token bucket, refilled at max_per_s
  int64_t tokens_ms_ = 0;
  struct Holders {
    std::vector<SimHolder> list;  // largest first
    int64_t refreshed_ms = 0;
  };
  std::map<Pubkey, Holders> holders_;  // worker thread only
};

}  // namespace arb::app
