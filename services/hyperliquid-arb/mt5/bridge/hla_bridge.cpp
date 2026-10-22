// hla_bridge.dll: loaded by the HlaPublisher and HlaExecutor MQL5 services inside
// terminal64.exe. Both services share one mapping per process; every call checks the
// magic so an engine restart (which clears it) never sees a half-written ring.
#include "hla/bridge_layout.hpp"

#include <cmath>
#include <mutex>
#include <string>

#ifdef _WIN32
#define HLA_EXPORT __declspec(dllexport)
#else
#define HLA_EXPORT
#endif

namespace {

using namespace hla::bridge;

std::mutex open_mutex;
SharedMemory memory;
std::atomic<Layout*> mapped{nullptr};
int references = 0;

Layout* live() noexcept {
  auto* layout = mapped.load(std::memory_order_acquire);
  if (layout == nullptr || layout->magic.load(std::memory_order_acquire) != kMagic) return nullptr;
  return layout;
}

bool valid_index(const Layout* layout, int index) noexcept {
  return index >= 0 && static_cast<std::uint32_t>(index) < layout->symbol_count;
}

std::string narrow(const wchar_t* text) {
  std::string out;
  for (; text != nullptr && *text != 0; ++text) out.push_back(static_cast<char>(*text & 0x7F));
  return out;
}

}  // namespace

extern "C" {

// 1 open, 0 engine not running yet (retry), -1 version mismatch. Each call that returns 1
// takes one reference; call hla_close once for it. The mapping outlives engine restarts.
HLA_EXPORT int hla_open(const wchar_t* name) {
  std::lock_guard lock(open_mutex);
  if (auto* layout = mapped.load()) {
    if (layout->magic.load(std::memory_order_acquire) != kMagic) return 0;
    if (layout->version != kVersion) return -1;
    ++references;
    return 1;
  }
  if (!memory.open(narrow(name).c_str(), sizeof(Layout))) return 0;
  auto* layout = static_cast<Layout*>(memory.data());
  if (layout->magic.load(std::memory_order_acquire) != kMagic) {
    memory.close();
    return 0;
  }
  if (layout->version != kVersion) {
    memory.close();
    return -1;
  }
  references = 1;
  mapped.store(layout, std::memory_order_release);
  return 1;
}

// Bumped by every engine start; each MQL5 service keeps its own copy and re-reads the
// symbol list when it changes. 0 while the engine is absent.
HLA_EXPORT long long hla_generation() {
  auto* layout = live();
  return layout == nullptr ? 0 : static_cast<long long>(layout->generation.load());
}

HLA_EXPORT int hla_symbol_count() {
  auto* layout = live();
  return layout == nullptr ? 0 : static_cast<int>(layout->symbol_count);
}

HLA_EXPORT int hla_symbol_name(int index, wchar_t* buffer, int capacity) {
  auto* layout = live();
  if (layout == nullptr || !valid_index(layout, index) || buffer == nullptr || capacity <= 0) return 0;
  const char* name = layout->specs[index].name;
  int n = 0;
  for (; name[n] != '\0' && n + 1 < capacity; ++n) buffer[n] = static_cast<wchar_t>(name[n]);
  buffer[n] = 0;
  return n;
}

HLA_EXPORT int hla_publish_spec(int index, double contract_size, double volume_step, double volume_min,
                                double volume_max, double tick_size, double tick_value, int digits, int trade_mode,
                                int filling_mode, int session_open) {
  auto* layout = live();
  if (layout == nullptr || !valid_index(layout, index)) return 0;
  auto& s = layout->specs[index];
  const auto seq = s.seq.load(std::memory_order_relaxed);
  s.seq.store(seq + 1, std::memory_order_relaxed);
  std::atomic_thread_fence(std::memory_order_release);
  s.contract_size.store(contract_size, std::memory_order_relaxed);
  s.volume_step.store(volume_step, std::memory_order_relaxed);
  s.volume_min.store(volume_min, std::memory_order_relaxed);
  s.volume_max.store(volume_max, std::memory_order_relaxed);
  s.tick_size.store(tick_size, std::memory_order_relaxed);
  s.tick_value.store(tick_value, std::memory_order_relaxed);
  s.digits.store(digits, std::memory_order_relaxed);
  s.trade_mode.store(trade_mode, std::memory_order_relaxed);
  s.filling_mode.store(filling_mode, std::memory_order_relaxed);
  s.session_open.store(session_open, std::memory_order_relaxed);
  s.published.store(1, std::memory_order_relaxed);
  s.seq.store(seq + 2, std::memory_order_release);
  return 1;
}

// 1 pushed, 0 ring full or engine absent.
HLA_EXPORT int hla_push_tick(int index, double bid, double ask, long long time_msc) {
  auto* layout = live();
  if (layout == nullptr || !valid_index(layout, index)) return 0;
  return layout->ticks.push({static_cast<std::uint32_t>(index), 0, bid, ask, time_msc, wall_ns()}) ? 1 : 0;
}

// 1 with an order to execute, 0 when none. Expired orders are answered here with
// status `expired` and never reach the terminal.
HLA_EXPORT int hla_pop_order(long long* request_id, int* index, int* side, double* volume, int* deviation_points) {
  auto* layout = live();
  if (layout == nullptr) return 0;
  OrderRequest order{};
  while (layout->orders.pop(order)) {
    if (wall_ns() > order.expire_ns) {
      layout->txns.push({order.request_id, order.symbol, order.side, expired, 0, 0, 0, 0, 0, 0, wall_ns()});
      continue;
    }
    *request_id = static_cast<long long>(order.request_id);
    *index = static_cast<int>(order.symbol);
    *side = static_cast<int>(order.side);
    *volume = order.volume;
    *deviation_points = order.deviation_points;
    return 1;
  }
  return 0;
}

HLA_EXPORT int hla_push_txn(long long request_id, int index, int side, int status, int retcode, double volume,
                            double price, double commission, long long deal, long long order) {
  auto* layout = live();
  if (layout == nullptr) return 0;
  return layout->txns.push({static_cast<std::uint64_t>(request_id), static_cast<std::uint32_t>(index),
                            static_cast<std::uint32_t>(side), static_cast<std::uint32_t>(status),
                            static_cast<std::uint32_t>(retcode), volume, price, commission,
                            static_cast<std::uint64_t>(deal), static_cast<std::uint64_t>(order), wall_ns()})
             ? 1
             : 0;
}

HLA_EXPORT void hla_account(long long login, double balance, double equity, double margin_free, int connected,
                            int trade_allowed, int margin_mode) {
  auto* layout = live();
  if (layout == nullptr) return;
  auto& a = layout->account;
  const auto seq = a.seq.load(std::memory_order_relaxed);
  a.seq.store(seq + 1, std::memory_order_relaxed);
  std::atomic_thread_fence(std::memory_order_release);
  a.login.store(login, std::memory_order_relaxed);
  a.balance.store(balance, std::memory_order_relaxed);
  a.equity.store(equity, std::memory_order_relaxed);
  a.margin_free.store(margin_free, std::memory_order_relaxed);
  a.connected.store(connected, std::memory_order_relaxed);
  a.trade_allowed.store(trade_allowed, std::memory_order_relaxed);
  a.margin_mode.store(margin_mode, std::memory_order_relaxed);
  a.updated_ns.store(wall_ns(), std::memory_order_relaxed);
  a.seq.store(seq + 2, std::memory_order_release);
}

HLA_EXPORT void hla_position(int index, double net_lots) {
  auto* layout = live();
  if (layout == nullptr || !valid_index(layout, index)) return;
  layout->account.positions[index].store(net_lots, std::memory_order_relaxed);
}

// Marks a complete position scan (all symbols written since the previous call).
HLA_EXPORT void hla_positions_done() {
  auto* layout = live();
  if (layout != nullptr) layout->account.positions_ns.store(wall_ns(), std::memory_order_release);
}

HLA_EXPORT void hla_heartbeat(int role) {
  auto* layout = live();
  if (layout == nullptr) return;
  (role == 0 ? layout->publisher_ns : layout->executor_ns).store(wall_ns(), std::memory_order_relaxed);
}

HLA_EXPORT long long hla_engine_age_ms() {
  auto* layout = live();
  if (layout == nullptr) return -1;
  const auto engine = layout->engine_ns.load(std::memory_order_relaxed);
  const auto now = wall_ns();
  return engine == 0 || now < engine ? 0 : static_cast<long long>((now - engine) / 1'000'000);
}

HLA_EXPORT void hla_close() {
  std::lock_guard lock(open_mutex);
  if (references > 0) --references;
  if (references > 0) return;
  mapped.store(nullptr, std::memory_order_release);
  memory.close();
}

}  // extern "C"
