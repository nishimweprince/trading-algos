#include "bpmm/engine.hpp"

#include "bpmm/parser.hpp"

#include <algorithm>

namespace bpmm {

Engine::Engine(StrategyConfig strategy, RiskLimits risk, std::uint64_t shadow_latency_ns,
               std::int64_t maker_fee_ppm)
    : strategy_(strategy), risk_(risk), venue_(shadow_latency_ns, maker_fee_ppm) {
  risk_state_.peak_equity_raw = 10'000 * kScale;
  risk_state_.equity_raw = risk_state_.peak_equity_raw;
}

bool Engine::on_snapshot(const Snapshot& snapshot) noexcept {
  return book_.apply_snapshot(snapshot.last_update_id,
                              {snapshot.bids.data(), snapshot.bid_count},
                              {snapshot.asks.data(), snapshot.ask_count});
}

IntentBatch Engine::on_depth(const DepthEvent& event, std::uint64_t now_ns) noexcept {
  ++metrics_.depth_events;
  risk_state_.book_receive_ns = event.receive_ns;
  if (!book_.apply(event)) {
    on_gap();
    return {};
  }
  if (!durable_halt_ && risk_.halted()) risk_.acknowledge();
  risk_state_.book_verified = true;
  venue_.advance(now_ns, book_);
  auto intents = strategy_.quote(book_, bp_, regression_, position_, now_ns);
  IntentBatch accepted;
  risk_state_.open_orders = venue_.open_orders();
  risk_state_.oldest_pending_ns = venue_.oldest_pending_ns();
  risk_state_.inventory_raw = position_.quantity_raw;
  const auto flow_total = aggressive_buy_raw_ + aggressive_sell_raw_;
  risk_state_.toxicity_ppm = flow_total == 0 ? 0 : static_cast<std::int64_t>(
      (static_cast<__int128>(aggressive_buy_raw_ - aggressive_sell_raw_) * 1'000'000) / flow_total);
  const auto mid = Price{(book_.best_bid().price.raw + book_.best_ask().price.raw) / 2};
  if (strategy_.barrier_exit(position_, mid, event.event_ms)) {
    venue_.cancel_all();
    venue_.close_position(position_, book_, now_ns);
    apply_fills();
    cooldown_until_ms_ = event.event_ms + strategy_.config().cooldown_ms;
    return {};
  }
  if (event.event_ms < cooldown_until_ms_) {
    venue_.cancel_all();
    return {};
  }
  for (std::size_t i = 0; i < intents.size; ++i) {
    const auto& intent = intents.values[i];
    const auto code = risk_.check(intent, book_.best_bid().price, book_.best_ask().price,
                                  risk_state_, now_ns, event.event_ms);
    if (code == RiskCode::ok) {
      venue_.submit(intent, book_);
      strategy_.accepted(intent);
      accepted.push(intent);
      ++metrics_.intents;
    } else {
      ++metrics_.risk_rejects;
    }
  }
  return accepted;
}

void Engine::on_trade(const TradeEvent& event) noexcept {
  ++metrics_.trade_events;
  if (flow_window_ms_ == 0 || event.trade_ms >= flow_window_ms_ + 1'000) {
    aggressive_buy_raw_ = 0;
    aggressive_sell_raw_ = 0;
    flow_window_ms_ = event.trade_ms;
  }
  if (event.buyer_is_maker) aggressive_sell_raw_ += event.quantity.raw;
  else aggressive_buy_raw_ += event.quantity.raw;
  venue_.on_trade(event);
  apply_fills();
}

bool Engine::on_candle(const CandleEvent& event) noexcept {
  const auto accepted = bp_.on_closed(event);
  if (accepted) ++metrics_.closed_candles;
  return accepted;
}

void Engine::apply_fills() noexcept {
  for (const auto& fill : venue_.fills()) {
    if (fill_event_count_ < fill_events_.size()) fill_events_[fill_event_count_++] = fill;
    const auto signed_qty = fill.side == Side::buy ? fill.quantity.raw : -fill.quantity.raw;
    const auto old_qty = position_.quantity_raw;
    const auto new_qty = old_qty + signed_qty;
    if (old_qty == 0 || (old_qty > 0) == (signed_qty > 0)) {
      const auto old_notional = mul_scaled(position_.average_entry.raw, std::llabs(old_qty));
      const auto add_notional = mul_scaled(fill.price.raw, std::llabs(signed_qty));
      position_.average_entry = new_qty == 0
                                    ? Price{}
                                    : Price{((old_notional + add_notional) * kScale) /
                                            std::max<std::int64_t>(1, std::llabs(new_qty))};
      if (old_qty == 0) position_.opened_ms = fill.fill_ns / 1'000'000;
    } else {
      const auto closed = std::min(std::llabs(old_qty), std::llabs(signed_qty));
      const auto per_unit = old_qty > 0 ? fill.price.raw - position_.average_entry.raw
                                        : position_.average_entry.raw - fill.price.raw;
      metrics_.realized_pnl_raw += mul_scaled(per_unit, closed) - fill.fee_raw;
      if (new_qty == 0) position_.average_entry = {};
      else if ((new_qty > 0) != (old_qty > 0)) position_.average_entry = fill.price;
    }
    position_.quantity_raw = new_qty;
    ++metrics_.fills;
  }
  venue_.clear_fills();
  risk_state_.realized_pnl_raw = metrics_.realized_pnl_raw;
  risk_state_.equity_raw = 10'000 * kScale + metrics_.realized_pnl_raw;
  risk_state_.peak_equity_raw = std::max(risk_state_.peak_equity_raw, risk_state_.equity_raw);
}

void Engine::on_gap() noexcept {
  ++metrics_.gaps;
  risk_.halt();
  risk_state_.book_verified = false;
  venue_.cancel_all();
}

void Engine::kill() noexcept {
  durable_halt_ = true;
  risk_.halt();
  risk_state_.halted = true;
  venue_.cancel_all();
}

void Engine::acknowledge_halt() noexcept {
  durable_halt_ = false;
  risk_state_.halted = false;
  if (book_.state() == BookState::verified) risk_.acknowledge();
}

bool Engine::ready() const noexcept {
  return book_.state() == BookState::verified && bp_.ready() && !risk_.halted();
}

std::uint64_t Engine::state_hash() const noexcept {
  auto hash = book_.state_hash();
  hash ^= static_cast<std::uint64_t>(position_.quantity_raw) + 0x9e3779b97f4a7c15ULL + (hash << 6) +
          (hash >> 2);
  hash ^= static_cast<std::uint64_t>(metrics_.realized_pnl_raw) + 0x9e3779b97f4a7c15ULL +
          (hash << 6) + (hash >> 2);
  hash ^= bp_.close_ms() + (hash << 6) + (hash >> 2);
  return hash;
}

}  // namespace bpmm
