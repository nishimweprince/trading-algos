#pragma once
// Preconfirmation -> speculative pool state (19 Nov 2026). For each preconfirmed transaction that touches pools we
// trade, a worker thread simulates it against the latest processed state (Helius simulateTransaction, sigVerify
// off) and reads back those pools' accounts as they would be after it. The engine thread then prices the pools'
// cycles on that state through StateStore::speculate and an engine overlay; the live state is never changed,
// because about two thirds of scheduled transactions never land (Astralane postpacks, 19 Nov).
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include "core/rpc.hpp"
#include "core/types.hpp"
#include "feed/preconf.hpp"
#include "obs/latency.hpp"
#include "state/state_store.hpp"

namespace arb::app {

struct OwnedUpdate {
  Pubkey key{}, owner{};
  std::vector<uint8_t> data;
  uint64_t slot = 0;
  bool missing = false;
};

struct SpecResult {
  std::vector<uint32_t> pools;
  std::vector<OwnedUpdate> accounts;
  uint64_t slot = 0;          // preconf slot
  uint64_t sim_slot = 0;      // simulation context slot
  uint64_t recv_ns = 0, sim_done_ns = 0;
  Signature sig{};
  std::string tx;  // the trigger's wire bytes (back-run bundle: [trigger, ours])
  /** Views for StateStore::speculate (valid while this result lives). */
  std::vector<AccountUpdate> views() const;
};

/** Pools a transaction touches: static keys plus lookup-table addresses resolved from `tables`. */
std::vector<uint32_t> touched_pools(const feed::TxKeys& k, const std::unordered_map<Pubkey, uint32_t, PubkeyHash>& pool_of,
                                    const std::unordered_map<Pubkey, std::vector<Pubkey>, PubkeyHash>& tables, std::vector<Pubkey>* missing_tables);

class PreconfSim {
 public:
  /**
   * `pool_of`: pool address -> pool id (the pools we trade); `keys_of`: pool id -> the accounts to read back.
   * Both are copied: the worker never touches the store.
   */
  PreconfSim(std::string rpc_url, std::unordered_map<Pubkey, uint32_t, PubkeyHash> pool_of,
             std::unordered_map<uint32_t, std::vector<Pubkey>> keys_of, feed::CreditGuard* guard, size_t max_queue = 64);
  ~PreconfSim();
  PreconfSim(const PreconfSim&) = delete;
  PreconfSim& operator=(const PreconfSim&) = delete;
  /** From the feed thread; never blocks (a full queue drops the oldest). */
  void push(feed::PreconfTx&& t);
  /** From the engine thread. */
  bool pop(SpecResult& out);
  uint64_t received() const { return in_.load(); }
  uint64_t no_pool() const { return no_pool_.load(); }
  uint64_t dropped() const { return dropped_.load(); }
  uint64_t sims() const { return sims_.load(); }
  uint64_t sim_failed() const { return sim_failed_.load(); }
  uint64_t sim_reverted() const { return sim_reverted_.load(); }
  uint64_t table_reads() const { return table_reads_.load(); }
  /** Preconf receipt to simulated state ready, us (1 h). */
  obs::LatencyStats sim_stats(uint64_t now) {
    std::lock_guard<std::mutex> g(lat_mu_);
    return sim_us_.stats(now);
  }

 private:
  void run();
  std::string rpc_url_;
  std::unordered_map<Pubkey, uint32_t, PubkeyHash> pool_of_;
  std::unordered_map<uint32_t, std::vector<Pubkey>> keys_of_;
  std::unordered_map<Pubkey, std::vector<Pubkey>, PubkeyHash> tables_;  // worker thread only
  feed::CreditGuard* guard_;
  size_t max_queue_;
  std::mutex mu_, out_mu_;
  std::condition_variable cv_;
  std::deque<feed::PreconfTx> q_;
  std::deque<SpecResult> out_;
  std::atomic<bool> stop_{false};
  std::atomic<uint64_t> in_{0}, no_pool_{0}, dropped_{0}, sims_{0}, sim_failed_{0}, sim_reverted_{0}, table_reads_{0};
  std::mutex lat_mu_;
  obs::LatencyWindow sim_us_{3600};
  std::thread th_;
};

}  // namespace arb::app
