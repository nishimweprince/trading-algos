#pragma once
// The per-pool snapshot the quoters read: the pool account plus everything its
// swap math needs (vault balances, tick arrays, mints), stamped with slots.
// Immutable once published; readers hold a shared_ptr<const PoolState>.
#include <array>
#include <atomic>
#include <cstdint>
#include <memory>
#include <optional>
#include <string_view>

#include "core/types.hpp"
#include "state/accounts.hpp"

namespace arb {

/** DEX enum; the values are the executor's LegSpec.dex encoding (spec §8). */
enum class DexKind : uint8_t { RaydiumV4 = 0, Whirlpool = 1, Cpmm = 2, Dlmm = 3, Clmm = 4, PumpSwap = 5 };
const char* dex_name(DexKind k);
/** The kind named by dex_name, or nullopt. */
std::optional<DexKind> dex_kind_from_name(std::string_view name);
/** The DEX's program ID, or nullptr for a kind this build does not support yet. */
const Pubkey* dex_program(DexKind k);
/** Constant-product pools: the vault balances (less any fees owed) are the reserves. */
constexpr bool is_constant_product(DexKind k) { return k == DexKind::RaydiumV4 || k == DexKind::Cpmm || k == DexKind::PumpSwap; }

/** RCU pointer: std::atomic<std::shared_ptr> where the library has it (libstdc++), else a spinlocked copy (libc++). */
template <class T>
class RcuPtr {
 public:
  std::shared_ptr<const T> load() const {
#if defined(__cpp_lib_atomic_shared_ptr)
    return p_.load(std::memory_order_acquire);
#else
    while (lock_.test_and_set(std::memory_order_acquire)) {
    }
    std::shared_ptr<const T> out = p_;
    lock_.clear(std::memory_order_release);
    return out;
#endif
  }
  void store(std::shared_ptr<const T> v) {
#if defined(__cpp_lib_atomic_shared_ptr)
    p_.store(std::move(v), std::memory_order_release);
#else
    while (lock_.test_and_set(std::memory_order_acquire)) {
    }
    p_.swap(v);
    lock_.clear(std::memory_order_release);
    // the old value is released here, outside the lock
#endif
  }

 private:
#if defined(__cpp_lib_atomic_shared_ptr)
  std::atomic<std::shared_ptr<const T>> p_;
#else
  mutable std::atomic_flag lock_ = ATOMIC_FLAG_INIT;
  std::shared_ptr<const T> p_;
#endif
};

/** One tick-array slot of a Whirlpool snapshot. */
struct TickArraySlot {
  int32_t start = 0;
  enum : uint8_t { Unknown = 0, Loaded = 1, Missing = 2 } state = Unknown;  // Missing: PDA has no account
  std::shared_ptr<const state::TickArray> array;
  std::shared_ptr<const state::ClmmTickArray> clmm;  // Raydium CLMM pools
  Pubkey key{};
};

constexpr int MAX_TICK_ARRAYS = 7;  // current array base -3 .. +3

struct PoolState {
  DexKind kind = DexKind::RaydiumV4;
  Pubkey address{};
  uint32_t id = 0;
  // Token "a" is Raydium's coin / Whirlpool's token A; "b" is pc / token B.
  Pubkey mint_a{}, mint_b{};
  Pubkey vault_a{}, vault_b{};
  uint64_t vault_a_amount = 0, vault_b_amount = 0;
  std::shared_ptr<const state::Mint> mint_a_info, mint_b_info;  // null until seen
  state::RaydiumV4 ray;
  state::Whirlpool wp;
  state::CpmmPool cpmm;
  state::CpmmConfig cpmm_cfg;
  state::ClmmPool clmm;
  state::ClmmConfig clmm_cfg;
  std::shared_ptr<const state::ClmmBitmapExtension> clmm_bitmap;
  state::PumpPool pump;
  state::PumpGlobalConfig pump_cfg;
  std::shared_ptr<const state::PumpFeeConfig> pump_fees;
  bool pump_canonical = false;  // a pump-migrated pool: the market-cap fee tiers apply
  std::array<TickArraySlot, MAX_TICK_ARRAYS> ticks{};
  uint8_t n_ticks = 0;
  // Freshness: slots of the pool account and of the vaults, newest component slot, store sequence.
  uint64_t pool_slot = 0, vault_a_slot = 0, vault_b_slot = 0;
  uint64_t slot = 0;           // newest component slot
  uint64_t write_version = 0;  // of the pool account
  uint64_t seq = 0;
  bool complete = false;       // pool, both vaults and both mints seen
  bool awaiting_push = false;  // pool or a vault was last set by a resync read

  /**
   * Quotable (spec §4): every component seen, and the pool account and both vaults within
   * max_age slots of each other (a swap writes all three in one slot). Tick arrays change only
   * when crossed, so any received copy is current until the next push.
   */
  bool quotable(uint64_t max_age) const;
  const TickArraySlot* tick_slot(int32_t start) const {
    for (uint8_t i = 0; i < n_ticks; ++i)
      if (ticks[i].start == start) return &ticks[i];
    return nullptr;
  }
};

}  // namespace arb
