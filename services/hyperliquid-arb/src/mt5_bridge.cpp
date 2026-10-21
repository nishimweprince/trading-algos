#include "hla/mt5_bridge.hpp"

#include <cstring>
#include <new>

namespace hla {

bool Mt5Bridge::create(const std::string& name, std::span<const std::string> symbols, std::string& error) {
  if (symbols.size() > bridge::kSymbols) {
    error = "bridge: more symbols than kSymbols";
    return false;
  }
  if (!memory_.create(name.c_str(), sizeof(bridge::Layout))) {
    error = "bridge: cannot create shared memory " + name;
    return false;
  }
  auto* layout = static_cast<bridge::Layout*>(memory_.data());
  // Readers check magic before every access; clearing it first fences a restart. The rings
  // are reset too, so an order queued by a previous engine can never execute.
  layout->magic.store(0, std::memory_order_release);
  const auto generation = layout->generation.load() + 1;
  std::memset(static_cast<void*>(layout), 0, sizeof(bridge::Layout));
  new (layout) bridge::Layout{};
  layout->version = bridge::kVersion;
  layout->symbol_count = static_cast<std::uint32_t>(symbols.size());
  for (std::size_t i = 0; i < symbols.size(); ++i) {
    if (symbols[i].size() >= bridge::kNameBytes) {
      error = "bridge: MT5 symbol name too long: " + symbols[i];
      return false;
    }
    std::memcpy(layout->specs[i].name, symbols[i].c_str(), symbols[i].size() + 1);
  }
  layout->generation.store(generation);
  layout->magic.store(bridge::kMagic, std::memory_order_release);
  layout_ = layout;
  return true;
}

Mt5Spec Mt5Bridge::spec(std::size_t symbol) const noexcept {
  Mt5Spec out;
  const auto& s = layout_->specs[symbol];
  for (int attempt = 0; attempt < 16; ++attempt) {
    const auto before = s.seq.load(std::memory_order_acquire);
    if ((before & 1U) != 0) continue;
    out.valid = s.published.load(std::memory_order_relaxed) != 0;
    out.contract_size = s.contract_size.load(std::memory_order_relaxed);
    out.volume_step = s.volume_step.load(std::memory_order_relaxed);
    out.volume_min = s.volume_min.load(std::memory_order_relaxed);
    out.volume_max = s.volume_max.load(std::memory_order_relaxed);
    out.tick_size = s.tick_size.load(std::memory_order_relaxed);
    out.tick_value = s.tick_value.load(std::memory_order_relaxed);
    out.digits = s.digits.load(std::memory_order_relaxed);
    out.trade_mode = s.trade_mode.load(std::memory_order_relaxed);
    out.filling_mode = s.filling_mode.load(std::memory_order_relaxed);
    out.session_open = s.session_open.load(std::memory_order_relaxed) != 0;
    std::atomic_thread_fence(std::memory_order_acquire);
    if (s.seq.load(std::memory_order_relaxed) == before) return out;
  }
  return {};
}

Mt5Account Mt5Bridge::account() const noexcept {
  Mt5Account out;
  const auto& a = layout_->account;
  for (int attempt = 0; attempt < 16; ++attempt) {
    const auto before = a.seq.load(std::memory_order_acquire);
    if ((before & 1U) != 0) continue;
    out.login = a.login.load(std::memory_order_relaxed);
    out.balance = a.balance.load(std::memory_order_relaxed);
    out.equity = a.equity.load(std::memory_order_relaxed);
    out.margin_free = a.margin_free.load(std::memory_order_relaxed);
    out.connected = a.connected.load(std::memory_order_relaxed) != 0;
    out.trade_allowed = a.trade_allowed.load(std::memory_order_relaxed) != 0;
    out.margin_mode = a.margin_mode.load(std::memory_order_relaxed);
    out.updated_ns = a.updated_ns.load(std::memory_order_relaxed);
    std::atomic_thread_fence(std::memory_order_acquire);
    if (a.seq.load(std::memory_order_relaxed) == before) break;
  }
  out.valid = out.updated_ns != 0;
  for (std::size_t i = 0; i < bridge::kSymbols; ++i) out.positions[i] = a.positions[i].load(std::memory_order_relaxed);
  out.positions_ns = a.positions_ns.load(std::memory_order_acquire);
  return out;
}

}  // namespace hla
