#pragma once

#include "hla/bridge_layout.hpp"

#include <array>
#include <cstdint>
#include <span>
#include <string>

namespace hla {

struct Mt5Spec final {
  bool valid{};
  double contract_size{};
  double volume_step{};
  double volume_min{};
  double volume_max{};
  double tick_size{};
  double tick_value{};
  std::int32_t digits{};
  std::int32_t trade_mode{};
  std::int32_t filling_mode{};
  bool session_open{};
  // Account-currency P&L per lot per 1.0 price move.
  [[nodiscard]] double value_per_point() const noexcept { return tick_size > 0 ? tick_value / tick_size : 0; }
  [[nodiscard]] bool tradeable() const noexcept { return valid && trade_mode == 4 && session_open; }
};

struct Mt5Account final {
  bool valid{};
  std::int64_t login{};
  double balance{};
  double equity{};
  double margin_free{};
  bool connected{};
  bool trade_allowed{};
  std::int32_t margin_mode{};
  std::uint64_t updated_ns{};
  std::array<double, bridge::kSymbols> positions{};
  std::uint64_t positions_ns{};
};

// Engine side of the bridge: creates the mapping, owns the order ring (producer) and
// consumes ticks and transaction results.
class Mt5Bridge final {
 public:
  bool create(const std::string& name, std::span<const std::string> symbols, std::string& error);
  [[nodiscard]] bool ready() const noexcept { return layout_ != nullptr; }

  bool pop_tick(bridge::Tick& tick) noexcept { return layout_->ticks.pop(tick); }
  bool pop_txn(bridge::Txn& txn) noexcept { return layout_->txns.pop(txn); }
  bool push_order(const bridge::OrderRequest& order) noexcept { return layout_->orders.push(order); }
  void heartbeat(std::uint64_t now_ns) noexcept { layout_->engine_ns.store(now_ns, std::memory_order_relaxed); }

  [[nodiscard]] Mt5Spec spec(std::size_t symbol) const noexcept;
  [[nodiscard]] Mt5Account account() const noexcept;
  [[nodiscard]] std::uint64_t publisher_ns() const noexcept { return layout_->publisher_ns.load(); }
  [[nodiscard]] std::uint64_t executor_ns() const noexcept { return layout_->executor_ns.load(); }
  [[nodiscard]] bridge::Layout* layout() const noexcept { return layout_; }

 private:
  bridge::SharedMemory memory_;
  bridge::Layout* layout_{};
};

}  // namespace hla

// C ABI exported by hla_bridge.dll and called from MQL5 (#import "hla_bridge.dll").
// MQL5 int = int32, long = int64, string = const wchar_t* (UTF-16), arrays by reference.
// Also linked into the tests, which play the publisher and executor.
extern "C" {
int hla_open(const wchar_t* name);
long long hla_generation();
int hla_symbol_count();
int hla_symbol_name(int index, wchar_t* buffer, int capacity);
int hla_publish_spec(int index, double contract_size, double volume_step, double volume_min, double volume_max,
                     double tick_size, double tick_value, int digits, int trade_mode, int filling_mode,
                     int session_open);
int hla_push_tick(int index, double bid, double ask, long long time_msc);
int hla_pop_order(long long* request_id, int* index, int* side, double* volume, int* deviation_points);
int hla_push_txn(long long request_id, int index, int side, int status, int retcode, double volume, double price,
                 double commission, long long deal, long long order);
void hla_account(long long login, double balance, double equity, double margin_free, int connected,
                 int trade_allowed, int margin_mode);
void hla_position(int index, double net_lots);
void hla_positions_done();
void hla_heartbeat(int role);  // 0 publisher, 1 executor
long long hla_engine_age_ms();
void hla_close();
}
