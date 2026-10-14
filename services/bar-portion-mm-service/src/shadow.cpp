#include "bpmm/shadow.hpp"

#include <algorithm>

namespace bpmm {

ShadowOrder* ShadowVenue::find_slot(Side side, std::uint8_t level) noexcept {
  ShadowOrder* empty = nullptr;
  for (auto& order : orders_) {
    if ((order.state == ShadowOrderState::resting || order.state == ShadowOrderState::partial ||
         order.state == ShadowOrderState::pending) && order.side == side && order.level == level)
      return &order;
    if (empty == nullptr && (order.state == ShadowOrderState::empty ||
                             order.state == ShadowOrderState::filled ||
                             order.state == ShadowOrderState::canceled ||
                             order.state == ShadowOrderState::rejected))
      empty = &order;
  }
  return empty;
}

void ShadowVenue::submit(const QuoteIntent& intent, const OrderBook& book) noexcept {
  if (intent.kind == IntentKind::cancel_all) {
    cancel_all();
    return;
  }
  if (intent.kind != IntentKind::place) return;
  auto* slot = find_slot(intent.side, intent.level);
  if (slot == nullptr) return;
  if (slot->state == ShadowOrderState::resting || slot->state == ShadowOrderState::partial ||
      slot->state == ShadowOrderState::pending)
    slot->state = ShadowOrderState::canceled;
  *slot = {next_order_id_++, intent.side, intent.level, intent.price, intent.quantity,
           intent.quantity, {}, intent.decision_ns + latency_ns_, ShadowOrderState::pending};
  const auto bid = book.best_bid().price;
  const auto ask = book.best_ask().price;
  if ((intent.side == Side::buy && intent.price.raw >= ask.raw) ||
      (intent.side == Side::sell && intent.price.raw <= bid.raw))
    slot->state = ShadowOrderState::rejected;
}

void ShadowVenue::advance(std::uint64_t now_ns, const OrderBook& book) noexcept {
  for (auto& order : orders_) {
    if (order.state != ShadowOrderState::pending || order.arrival_ns > now_ns) continue;
    const auto bid = book.best_bid().price;
    const auto ask = book.best_ask().price;
    if ((order.side == Side::buy && order.price.raw >= ask.raw) ||
        (order.side == Side::sell && order.price.raw <= bid.raw)) {
      order.state = ShadowOrderState::rejected;
      continue;
    }
    order.queue_ahead = book.quantity_at(order.side, order.price);
    order.state = ShadowOrderState::resting;
  }
}

void ShadowVenue::fill(ShadowOrder& order, Quantity quantity, std::uint64_t fill_ns) noexcept {
  if (quantity.raw <= 0 || fill_count_ == fills_.size()) return;
  quantity.raw = std::min(quantity.raw, order.remaining.raw);
  order.remaining.raw -= quantity.raw;
  order.state = order.remaining.raw == 0 ? ShadowOrderState::filled : ShadowOrderState::partial;
  const auto notional = mul_scaled(order.price.raw, quantity.raw);
  const auto fee = static_cast<std::int64_t>((static_cast<__int128>(notional) * maker_fee_ppm_) /
                                             1'000'000);
  fills_[fill_count_++] = {order.id, order.side, order.price, quantity, fee, fill_ns};
}

void ShadowVenue::on_trade(const TradeEvent& trade) noexcept {
  for (auto& order : orders_) {
    if (order.state != ShadowOrderState::resting && order.state != ShadowOrderState::partial) continue;
    const bool aggressor_sell = trade.buyer_is_maker;
    const bool touches = (order.side == Side::buy && aggressor_sell && trade.price.raw <= order.price.raw) ||
                         (order.side == Side::sell && !aggressor_sell && trade.price.raw >= order.price.raw);
    if (!touches) continue;
    if (trade.price == order.price && order.queue_ahead.raw > 0) {
      const auto consumed = std::min(order.queue_ahead.raw, trade.quantity.raw);
      order.queue_ahead.raw -= consumed;
      const auto excess = trade.quantity.raw - consumed;
      if (excess > 0) fill(order, Quantity{excess}, trade.receive_ns);
    } else {
      fill(order, order.remaining, trade.receive_ns);
    }
  }
}

void ShadowVenue::close_position(const PositionState& position, const OrderBook& book,
                                 std::uint64_t now_ns) noexcept {
  if (position.quantity_raw == 0 || fill_count_ == fills_.size()) return;
  const auto side = position.quantity_raw > 0 ? Side::sell : Side::buy;
  const auto touch = side == Side::sell ? book.best_bid() : book.best_ask();
  if (touch.price.raw <= 0) return;
  const auto position_quantity = static_cast<std::int64_t>(std::llabs(position.quantity_raw));
  const auto quantity = Quantity{std::min(position_quantity, touch.quantity.raw)};
  const auto notional = mul_scaled(touch.price.raw, quantity.raw);
  const auto taker_fee = static_cast<std::int64_t>((static_cast<__int128>(notional) * 500) / 1'000'000);
  fills_[fill_count_++] = ShadowFill{next_order_id_++, side, touch.price, quantity, taker_fee,
                                    now_ns + latency_ns_};
}

void ShadowVenue::cancel_all() noexcept {
  for (auto& order : orders_) {
    if (order.state == ShadowOrderState::pending || order.state == ShadowOrderState::resting ||
        order.state == ShadowOrderState::partial)
      order.state = ShadowOrderState::canceled;
  }
}

std::uint32_t ShadowVenue::open_orders() const noexcept {
  std::uint32_t count = 0;
  for (const auto& order : orders_) {
    if (order.state == ShadowOrderState::pending || order.state == ShadowOrderState::resting ||
        order.state == ShadowOrderState::partial)
      ++count;
  }
  return count;
}

std::uint64_t ShadowVenue::oldest_pending_ns() const noexcept {
  std::uint64_t oldest = 0;
  for (const auto& order : orders_) {
    if (order.state == ShadowOrderState::pending && (oldest == 0 || order.arrival_ns < oldest))
      oldest = order.arrival_ns;
  }
  return oldest;
}

}  // namespace bpmm
