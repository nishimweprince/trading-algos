#pragma once
// Single-writer StateStore (spec §4). The feed thread applies account updates;
// each update re-publishes the snapshot of every pool that depends on that
// account (RCU swap) and pushes the pool id onto an SPSC queue for the engine.
#include <atomic>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <unordered_map>
#include <variant>
#include <vector>

#include "core/error.hpp"
#include "core/types.hpp"
#include "state/pool_state.hpp"

namespace arb {

/** Bounded lock-free SPSC queue (power-of-two capacity). */
template <class T, size_t N>
class SpscQueue {
  static_assert((N & (N - 1)) == 0, "capacity must be a power of two");

 public:
  bool push(const T& v) {
    size_t h = head_.load(std::memory_order_relaxed);
    if (h - tail_.load(std::memory_order_acquire) >= N) return false;
    buf_[h & (N - 1)] = v;
    head_.store(h + 1, std::memory_order_release);
    return true;
  }
  bool pop(T& v) {
    size_t t = tail_.load(std::memory_order_relaxed);
    if (t == head_.load(std::memory_order_acquire)) return false;
    v = buf_[t & (N - 1)];
    tail_.store(t + 1, std::memory_order_release);
    return true;
  }
  size_t size() const { return head_.load() - tail_.load(); }

 private:
  alignas(64) std::atomic<size_t> head_{0};
  alignas(64) std::atomic<size_t> tail_{0};
  T buf_[N];
};

struct PubkeyHash {
  size_t operator()(const Pubkey& k) const { return size_t(key_hash(k)); }
};

/** What a subscribed account is, decided at registration. */
enum class AccountKind : uint8_t { RaydiumPool, Whirlpool, TickArray, Vault, Mint, KaminoReserve, Wallet, CpmmPool, CpmmConfig, PumpPool, PumpGlobalConfig,
                                    PumpFeeConfig };

/** A pool to track and the accounts it is built from. */
struct PoolDef {
  DexKind kind = DexKind::RaydiumV4;
  Pubkey address{};
  Pubkey mint_a{}, mint_b{}, vault_a{}, vault_b{};
  std::vector<std::pair<int32_t, Pubkey>> tick_arrays;  // Whirlpool: (start index, PDA)
  Pubkey config{};      // CPMM: the AmmConfig holding the fee rates; PumpSwap: the GlobalConfig (shared by many pools)
  Pubkey fee_config{};  // PumpSwap: the Pump Fees FeeConfig (market-cap fee tiers)
};

struct AccountUpdate {
  Pubkey key{};
  Pubkey owner{};
  const uint8_t* data = nullptr;
  size_t len = 0;
  uint64_t slot = 0;
  uint64_t write_version = 0;
  bool missing = false;      // the account does not exist (resync returned null)
  bool from_resync = false;  // an RPC read, not a stream push (spec §4: quotable only after a fresh push)
};

class StateStore {
 public:
  StateStore();
  /** Register a pool and its accounts (discovery, off the hot path). Returns the pool id. */
  uint32_t add_pool(const PoolDef& def);
  void add_reserve(const Pubkey& reserve);
  /** Apply one account update. Returns false (and counts) on a decode failure. */
  bool apply(const AccountUpdate& u);
  /**
   * The account's current value may come from a candidate block that loses (Alpenglow: several banks per
   * slot). Its pools stop being quotable until a newer push or a trusted resync replaces it.
   */
  void mark_suspect(const Pubkey& key);
  /** Engine side: next changed pool id. */
  bool pop_changed(uint32_t& pool_id) { return changed_.pop(pool_id); }

  std::shared_ptr<const PoolState> pool(uint32_t id) const { return pools_[id]->snap.load(); }
  size_t pool_count() const { return pools_.size(); }
  std::optional<uint32_t> pool_id(const Pubkey& address) const;
  const PoolDef& def(uint32_t id) const { return pools_[id]->def; }
  std::shared_ptr<const state::KaminoReserve> reserve(const Pubkey& key) const;
  std::shared_ptr<const state::Mint> mint(const Pubkey& key) const;
  /** Every account key the feed must subscribe to. */
  std::vector<Pubkey> subscribed_keys() const;
  uint64_t newest_slot() const { return newest_slot_.load(std::memory_order_relaxed); }
  uint64_t decode_failures() const { return decode_failures_.load(); }
  uint64_t updates() const { return updates_.load(); }

 private:
  struct AccountEntry {
    AccountKind kind = AccountKind::Vault;
    std::vector<uint32_t> pools;
    uint64_t slot = 0, write_version = 0;
    bool seen = false, missing = false;
    bool awaiting_push = false;  // last applied value came from a resync
    std::variant<std::monostate, state::RaydiumV4, state::Whirlpool, state::TokenAccount, state::CpmmPool, state::CpmmConfig,
                 state::PumpPool, state::PumpGlobalConfig>
        pool_data;
    std::shared_ptr<const state::PumpFeeConfig> pump_fees;
    std::shared_ptr<const state::TickArray> tick_array;
    std::shared_ptr<const state::Mint> mint;
    std::shared_ptr<const state::KaminoReserve> reserve;
    int32_t tick_start = 0;
  };
  struct PoolSlot {
    PoolDef def;
    RcuPtr<PoolState> snap;
    int8_t pump_canonical = -1;  // PumpSwap: pool.creator is the base mint's pump pool-authority PDA (-1 = not yet known)
  };
  AccountEntry& entry(const Pubkey& k, AccountKind kind);
  void rebuild(uint32_t id);

  std::vector<std::unique_ptr<PoolSlot>> pools_;
  std::unordered_map<Pubkey, uint32_t, PubkeyHash> pool_index_;
  std::unordered_map<Pubkey, AccountEntry, PubkeyHash> accounts_;
  SpscQueue<uint32_t, 1 << 16> changed_;
  std::atomic<uint64_t> newest_slot_{0}, decode_failures_{0}, updates_{0};
  uint64_t seq_ = 0;
};

}  // namespace arb
