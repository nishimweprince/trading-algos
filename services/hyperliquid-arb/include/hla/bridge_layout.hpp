#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <type_traits>

// Shared-memory layout between hlarb (engine) and hla_bridge.dll inside terminal64.exe.
// Compiled into both sides; any change bumps kVersion. One writer per field or ring:
//   engine     names, symbol_count, generation, orders (producer), engine_ns
//   publisher  ticks (producer), specs, account (all but positions), publisher_ns
//   executor   orders (consumer), txns (producer), account.positions, executor_ns
// Times are Unix wall-clock nanoseconds so they compare across processes.
namespace hla::bridge {

constexpr std::uint64_t kMagic = 0x31524241424C4148ULL;  // "HLABAR1\0"
constexpr std::uint32_t kVersion = 1;
constexpr std::size_t kSymbols = 32;
constexpr std::size_t kNameBytes = 32;

template <typename T, std::size_t N>
struct Ring final {
  static_assert((N & (N - 1)) == 0, "ring size must be a power of two");
  static_assert(std::is_trivially_copyable_v<T>);
  alignas(64) std::atomic<std::uint64_t> head;  // next write
  alignas(64) std::atomic<std::uint64_t> tail;  // next read
  alignas(64) T slots[N];

  bool push(const T& value) noexcept {
    const auto h = head.load(std::memory_order_relaxed);
    if (h - tail.load(std::memory_order_acquire) >= N) return false;
    slots[h & (N - 1)] = value;
    head.store(h + 1, std::memory_order_release);
    return true;
  }
  bool pop(T& value) noexcept {
    const auto t = tail.load(std::memory_order_relaxed);
    if (t == head.load(std::memory_order_acquire)) return false;
    value = slots[t & (N - 1)];
    tail.store(t + 1, std::memory_order_release);
    return true;
  }
};

struct Tick final {
  std::uint32_t symbol;
  std::uint32_t flags;
  double bid;
  double ask;
  std::int64_t time_msc;  // broker tick time
  std::uint64_t publish_ns;
};

enum Side : std::uint32_t { buy = 0, sell = 1 };

struct OrderRequest final {
  std::uint64_t request_id;
  std::uint32_t symbol;
  std::uint32_t side;
  double volume;  // lots
  std::int32_t deviation_points;
  std::int32_t reserved;
  std::uint64_t submit_ns;
  std::uint64_t expire_ns;  // the DLL drops the order unexecuted after this
};

enum TxnStatus : std::uint32_t { filled = 1, partial = 2, rejected = 3, error = 4, expired = 5 };

struct Txn final {
  std::uint64_t request_id;
  std::uint32_t symbol;
  std::uint32_t side;
  std::uint32_t status;
  std::uint32_t retcode;  // MqlTradeResult.retcode
  double volume;          // filled lots
  double price;
  double commission;
  std::uint64_t deal;
  std::uint64_t order;
  std::uint64_t publish_ns;
};

// Written by the publisher once a second. Relaxed atomics under a seqlock (seq odd while writing).
struct SymbolSpec final {
  char name[kNameBytes];  // engine, before magic is set
  std::atomic<std::uint32_t> seq;
  std::atomic<std::uint32_t> published;
  std::atomic<double> contract_size;
  std::atomic<double> volume_step;
  std::atomic<double> volume_min;
  std::atomic<double> volume_max;
  std::atomic<double> tick_size;
  std::atomic<double> tick_value;  // deposit currency per tick per lot
  std::atomic<std::int32_t> digits;
  std::atomic<std::int32_t> trade_mode;    // SYMBOL_TRADE_MODE_FULL == 4
  std::atomic<std::int32_t> filling_mode;
  std::atomic<std::int32_t> session_open;  // 1 while a trade session is open now
};

struct AccountState final {
  std::atomic<std::uint32_t> seq;
  std::atomic<std::int64_t> login;
  std::atomic<double> balance;
  std::atomic<double> equity;
  std::atomic<double> margin_free;
  std::atomic<std::int32_t> connected;
  std::atomic<std::int32_t> trade_allowed;
  std::atomic<std::int32_t> margin_mode;  // ACCOUNT_MARGIN_MODE_*: 0 netting, 2 hedging
  std::atomic<std::uint64_t> updated_ns;
  std::atomic<double> positions[kSymbols];  // executor: net lots with our magic, signed
  std::atomic<std::uint64_t> positions_ns;
};

struct Layout final {
  std::atomic<std::uint64_t> magic;
  std::uint32_t version;
  std::uint32_t symbol_count;
  std::atomic<std::uint64_t> generation;  // bumped on every engine start
  std::atomic<std::uint64_t> publisher_ns;
  std::atomic<std::uint64_t> executor_ns;
  std::atomic<std::uint64_t> engine_ns;
  SymbolSpec specs[kSymbols];
  AccountState account;
  Ring<Tick, 8192> ticks;
  Ring<OrderRequest, 256> orders;
  Ring<Txn, 256> txns;
};

static_assert(std::atomic<std::uint64_t>::is_always_lock_free && std::atomic<double>::is_always_lock_free,
              "cross-process atomics must be lock-free");
static_assert(std::is_standard_layout_v<Layout>);

std::uint64_t wall_ns() noexcept;

// Shared-memory mapping. Windows: a named file mapping ("Local\\name"); POSIX (tests and
// dev hosts): shm_open("/name"), using the part of `name` after the last backslash.
class SharedMemory final {
 public:
  SharedMemory() = default;
  ~SharedMemory();
  SharedMemory(const SharedMemory&) = delete;
  SharedMemory& operator=(const SharedMemory&) = delete;

  bool create(const char* name, std::size_t size) noexcept;  // create or open existing
  bool open(const char* name, std::size_t size) noexcept;    // fails when absent
  void close() noexcept;
  // POSIX: unlinks the name (tests). Windows: no-op, the mapping dies with its last handle.
  static void remove(const char* name) noexcept;
  [[nodiscard]] void* data() const noexcept { return data_; }

 private:
  void* data_{};
  [[maybe_unused]] void* handle_{};  // Windows mapping handle
  std::size_t size_{};
};

}  // namespace hla::bridge
