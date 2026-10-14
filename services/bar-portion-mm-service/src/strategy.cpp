#include "bpmm/strategy.hpp"

#include <algorithm>
#include <cmath>
#include <vector>

namespace bpmm {

RollingRegression::RollingRegression(std::size_t capacity) : values_(capacity) {}

void RollingRegression::push(double x, double y) noexcept {
  if (values_.empty()) return;
  if (size_ == values_.size()) {
    const auto old = values_[head_];
    sx_ -= old.x;
    sy_ -= old.y;
    sxx_ -= old.x * old.x;
    sxy_ -= old.x * old.y;
  } else {
    ++size_;
  }
  values_[head_] = {x, y};
  head_ = (head_ + 1) % values_.size();
  sx_ += x;
  sy_ += y;
  sxx_ += x * x;
  sxy_ += x * y;
}

Regression RollingRegression::fit() const noexcept {
  if (size_ < 2) return {};
  const auto n = static_cast<double>(size_);
  const auto denominator = n * sxx_ - sx_ * sx_;
  if (std::abs(denominator) < 1e-18) return {};
  const auto slope = (n * sxy_ - sx_ * sy_) / denominator;
  return {(sy_ - slope * sx_) / n, slope, true};
}

bool BarPortion::on_closed(const CandleEvent& candle) noexcept {
  if (!candle.closed || candle.close_ms <= close_ms_ || candle.high.raw < candle.low.raw) return false;
  const auto range = candle.high.raw - candle.low.raw;
  value_ = range == 0 ? 0.0 : static_cast<double>(candle.close.raw - candle.open.raw) /
                                      static_cast<double>(range);
  value_ = std::clamp(value_, -1.0, 1.0);
  if (previous_close_.raw > 0) {
    const auto move_ppm = static_cast<std::int64_t>(
        (static_cast<__int128>(std::llabs(candle.close.raw - previous_close_.raw)) * 1'000'000) /
        previous_close_.raw);
    volatility_ppm_ = volatility_ppm_ == 0 ? move_ppm : (volatility_ppm_ * 19 + move_ppm) / 20;
  }
  previous_close_ = candle.close;
  close_ms_ = candle.close_ms;
  ready_ = true;
  return true;
}

IntentBatch QuoteStrategy::quote(const OrderBook& book, const BarPortion& bp,
                                 const Regression& regression, const PositionState& position,
                                 std::uint64_t now_ns) noexcept {
  IntentBatch batch;
  if (book.state() != BookState::verified || !bp.ready()) return batch;
  const auto bid = book.best_bid().price.raw;
  const auto ask = book.best_ask().price.raw;
  if (bid <= 0 || ask <= bid) return batch;
  const auto mid = (bid + ask) / 2;

  auto prediction_ppm = static_cast<std::int64_t>(std::llround(regression.predict(bp.value()) * 1'000'000.0));
  prediction_ppm = std::clamp(prediction_ppm, -config_.forecast_clamp_ppm,
                             config_.forecast_clamp_ppm);
  const auto inventory_ratio = config_.max_inventory_raw == 0
                                   ? 0.0
                                   : static_cast<double>(position.quantity_raw) /
                                         static_cast<double>(config_.max_inventory_raw);
  const auto skew_ppm = static_cast<std::int64_t>(std::llround(
      std::clamp(inventory_ratio, -1.0, 1.0) * static_cast<double>(config_.inventory_skew_ppm)));
  const auto reference = mid + static_cast<std::int64_t>(
                                   (static_cast<__int128>(mid) * (prediction_ppm - skew_ppm)) / 1'000'000);

  const auto active_levels = std::min<std::uint8_t>(config_.levels, 2);
  for (std::uint8_t level = 0; level < active_levels; ++level) {
    const auto volatility_floor = bp.volatility_ppm() * config_.volatility_spread_multiplier;
    const auto distance_ppm = std::max(config_.base_spread_ppm, volatility_floor) +
                              static_cast<std::int64_t>(level) * config_.level_step_ppm;
    const auto offset = static_cast<std::int64_t>(
        (static_cast<__int128>(reference) * distance_ppm) / 1'000'000);
    auto desired_bid = round_down(Price{reference - offset}, config_.tick_size);
    auto desired_ask = round_up(Price{reference + offset}, config_.tick_size);
    if (desired_bid.raw >= ask) desired_bid = Price{ask - config_.tick_size.raw};
    if (desired_ask.raw <= bid) desired_ask = Price{bid + config_.tick_size.raw};

    const auto threshold = config_.tick_size.raw * config_.reprice_threshold_ticks;
    if (active_bid_[level].raw == 0 || std::llabs(desired_bid.raw - active_bid_[level].raw) >= threshold ||
        now_ns - active_bid_ns_[level] >= config_.max_quote_age_ns) {
      batch.push({IntentKind::place, Side::buy, level, desired_bid, config_.quote_quantity, false,
                  now_ns, ++sequence_});
    }
    if (active_ask_[level].raw == 0 || std::llabs(desired_ask.raw - active_ask_[level].raw) >= threshold ||
        now_ns - active_ask_ns_[level] >= config_.max_quote_age_ns) {
      batch.push({IntentKind::place, Side::sell, level, desired_ask, config_.quote_quantity, false,
                  now_ns, ++sequence_});
    }
  }
  return batch;
}

void QuoteStrategy::accepted(const QuoteIntent& intent) noexcept {
  if (intent.kind != IntentKind::place || intent.level >= 2) return;
  auto& active = intent.side == Side::buy ? active_bid_ : active_ask_;
  auto& active_ns = intent.side == Side::buy ? active_bid_ns_ : active_ask_ns_;
  active[intent.level] = intent.price;
  active_ns[intent.level] = intent.decision_ns;
}

bool QuoteStrategy::barrier_exit(const PositionState& position, Price mid,
                                 std::uint64_t now_ms) const noexcept {
  if (position.quantity_raw == 0 || position.average_entry.raw <= 0) return false;
  const auto signed_move_ppm = static_cast<std::int64_t>(
      (static_cast<__int128>(mid.raw - position.average_entry.raw) * 1'000'000) /
      position.average_entry.raw) * (position.quantity_raw > 0 ? 1 : -1);
  return signed_move_ppm <= -config_.stop_loss_ppm ||
         signed_move_ppm >= config_.take_profit_ppm ||
         now_ms >= position.opened_ms + config_.time_barrier_ms;
}

}  // namespace bpmm
