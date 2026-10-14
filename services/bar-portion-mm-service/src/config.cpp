#include "bpmm/config.hpp"

#include <charconv>
#include <cstdlib>
#include <stdexcept>
#include <string_view>

namespace bpmm {
namespace {

std::int64_t integer(const char* name, std::int64_t fallback) {
  const auto* raw = std::getenv(name);
  if (raw == nullptr) return fallback;
  const std::string_view text(raw);
  std::int64_t value = 0;
  const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), value);
  if (error != std::errc{} || end != text.data() + text.size())
    throw std::invalid_argument(std::string(name) + " must be an integer");
  return value;
}

std::uint64_t unsigned_integer(const char* name, std::uint64_t fallback) {
  const auto value = integer(name, static_cast<std::int64_t>(fallback));
  if (value < 0) throw std::invalid_argument(std::string(name) + " must be non-negative");
  return static_cast<std::uint64_t>(value);
}

Decimal decimal(const char* name, Decimal fallback) {
  const auto* raw = std::getenv(name);
  return raw == nullptr ? fallback : Decimal::parse(raw);
}

}  // namespace

RuntimeConfig load_runtime_config() {
  RuntimeConfig config;
  config.strategy.tick_size = decimal("BPMM_TICK_SIZE", config.strategy.tick_size);
  config.strategy.quantity_step = decimal("BPMM_QUANTITY_STEP", config.strategy.quantity_step);
  config.strategy.quote_quantity = decimal("BPMM_QUOTE_QUANTITY", config.strategy.quote_quantity);
  config.strategy.base_spread_ppm = integer("BPMM_BASE_SPREAD_PPM", config.strategy.base_spread_ppm);
  config.strategy.level_step_ppm = integer("BPMM_LEVEL_STEP_PPM", config.strategy.level_step_ppm);
  config.strategy.forecast_clamp_ppm = integer("BPMM_FORECAST_CLAMP_PPM", config.strategy.forecast_clamp_ppm);
  config.strategy.inventory_skew_ppm = integer("BPMM_INVENTORY_SKEW_PPM", config.strategy.inventory_skew_ppm);
  config.strategy.max_inventory_raw = decimal("BPMM_MAX_INVENTORY", Decimal{config.strategy.max_inventory_raw}).raw;
  config.strategy.stop_loss_ppm = integer("BPMM_STOP_LOSS_PPM", config.strategy.stop_loss_ppm);
  config.strategy.take_profit_ppm = integer("BPMM_TAKE_PROFIT_PPM", config.strategy.take_profit_ppm);
  config.strategy.time_barrier_ms = unsigned_integer("BPMM_TIME_BARRIER_MS", config.strategy.time_barrier_ms);
  config.strategy.cooldown_ms = unsigned_integer("BPMM_COOLDOWN_MS", config.strategy.cooldown_ms);
  config.strategy.max_quote_age_ns = unsigned_integer("BPMM_MAX_QUOTE_AGE_NS", config.strategy.max_quote_age_ns);
  config.risk.max_inventory_raw = config.strategy.max_inventory_raw;
  config.risk.max_order_notional_raw = decimal("BPMM_MAX_ORDER_NOTIONAL", Decimal{config.risk.max_order_notional_raw}).raw;
  config.risk.max_open_orders = static_cast<std::uint32_t>(unsigned_integer("BPMM_MAX_OPEN_ORDERS", config.risk.max_open_orders));
  config.risk.max_orders_per_second = static_cast<std::uint32_t>(unsigned_integer("BPMM_MAX_ORDERS_PER_SECOND", config.risk.max_orders_per_second));
  config.risk.daily_loss_raw = decimal("BPMM_DAILY_LOSS", Decimal{config.risk.daily_loss_raw}).raw;
  config.risk.max_drawdown_raw = decimal("BPMM_MAX_DRAWDOWN", Decimal{config.risk.max_drawdown_raw}).raw;
  config.risk.max_book_age_ns = unsigned_integer("BPMM_MAX_BOOK_AGE_NS", config.risk.max_book_age_ns);
  config.risk.funding_guard_ms = unsigned_integer("BPMM_FUNDING_GUARD_MS", config.risk.funding_guard_ms);
  config.shadow_latency_ns = unsigned_integer("BPMM_SHADOW_LATENCY_NS", config.shadow_latency_ns);
  config.maker_fee_ppm = integer("BPMM_MAKER_FEE_PPM", config.maker_fee_ppm);
  if (config.strategy.tick_size.raw <= 0 || config.strategy.quantity_step.raw <= 0 ||
      config.strategy.quote_quantity.raw <= 0 || config.strategy.max_inventory_raw <= 0)
    throw std::invalid_argument("tick, quantity step, quote quantity, and max inventory must be positive");
  return config;
}

}  // namespace bpmm
