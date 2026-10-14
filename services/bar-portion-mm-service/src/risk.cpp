#include "bpmm/risk.hpp"

#include <algorithm>
#include <cstdlib>

namespace bpmm {

RiskCode RiskEngine::check(const QuoteIntent& intent, Price best_bid, Price best_ask,
                           const RiskSnapshot& state, std::uint64_t now_ns,
                           std::uint64_t now_ms) noexcept {
  if (halted_ || state.halted) return RiskCode::halted;
  if (!state.book_verified) return RiskCode::book_unverified;
  if (now_ns < state.book_receive_ns || now_ns - state.book_receive_ns > limits_.max_book_age_ns)
    return RiskCode::stale_book;
  if (std::llabs(state.inventory_raw) > limits_.max_inventory_raw) return RiskCode::inventory_limit;
  if (intent.kind == IntentKind::place && !intent.reduce_only) {
    const auto projected = state.inventory_raw +
        (intent.side == Side::buy ? intent.quantity.raw : -intent.quantity.raw);
    if (std::llabs(projected) > limits_.max_inventory_raw) return RiskCode::inventory_limit;
    if ((intent.side == Side::sell && state.toxicity_ppm > limits_.toxicity_limit_ppm) ||
        (intent.side == Side::buy && state.toxicity_ppm < -limits_.toxicity_limit_ppm))
      return RiskCode::toxicity_limit;
    const auto mid = (best_bid.raw + best_ask.raw) / 2;
    const auto exposure = std::llabs(mul_scaled(projected, mid));
    if (state.equity_raw <= 0) return RiskCode::margin_limit;
    const auto leverage_ppm = static_cast<std::int64_t>(
        (static_cast<__int128>(exposure) * 1'000'000) / state.equity_raw);
    if (leverage_ppm > limits_.max_leverage_ppm) return RiskCode::leverage_limit;
    const auto capacity = static_cast<std::int64_t>(
        (static_cast<__int128>(state.equity_raw) * limits_.max_leverage_ppm) / 1'000'000);
    const auto utilization_ppm = capacity <= 0 ? 1'000'001 : static_cast<std::int64_t>(
        (static_cast<__int128>(exposure) * 1'000'000) / capacity);
    if (utilization_ppm > limits_.max_margin_utilization_ppm) return RiskCode::margin_limit;
  }
  if (state.oldest_pending_ns != 0 && now_ns >= state.oldest_pending_ns &&
      now_ns - state.oldest_pending_ns > limits_.max_unacknowledged_ns)
    return RiskCode::unacknowledged_order;
  if (state.open_orders >= limits_.max_open_orders && intent.kind == IntentKind::place)
    return RiskCode::open_order_limit;
  if (state.realized_pnl_raw <= -limits_.daily_loss_raw) return RiskCode::daily_loss_limit;
  if (state.peak_equity_raw - state.equity_raw >= limits_.max_drawdown_raw)
    return RiskCode::drawdown_limit;
  if (state.consecutive_rejects >= limits_.max_consecutive_rejects) return RiskCode::reject_limit;
  if (state.next_funding_ms > now_ms && state.next_funding_ms - now_ms <= limits_.funding_guard_ms)
    return RiskCode::funding_guard;
  if (best_bid.raw <= 0 || best_ask.raw <= best_bid.raw) return RiskCode::book_unverified;
  const auto mid = (best_bid.raw + best_ask.raw) / 2;
  const auto spread_ppm = static_cast<std::int64_t>(
      (static_cast<__int128>(best_ask.raw - best_bid.raw) * 1'000'000) / mid);
  if (spread_ppm > limits_.max_spread_ppm) return RiskCode::spread_limit;
  const auto notional = std::llabs(mul_scaled(intent.price.raw, intent.quantity.raw));
  if (notional > limits_.max_order_notional_raw) return RiskCode::order_notional_limit;

  std::uint32_t recent = 0;
  for (const auto time : order_times_) {
    if (time != 0 && now_ns >= time && now_ns - time < 1'000'000'000ULL) ++recent;
  }
  if (recent >= limits_.max_orders_per_second) return RiskCode::order_rate_limit;
  order_times_[order_time_head_++ % order_times_.size()] = now_ns;
  return RiskCode::ok;
}

}  // namespace bpmm
