#include "cxa/book.hpp"

#include <algorithm>

namespace cxa {

bool OrderBook::update(Levels& levels, std::size_t& size, Level level, bool descending) noexcept {
  const auto better = [descending](std::int64_t lhs, std::int64_t rhs) {
    return descending ? lhs > rhs : lhs < rhs;
  };
  // Binary search for the first level not better than `level`.
  std::size_t lo = 0;
  std::size_t hi = size;
  while (lo < hi) {
    const auto mid = lo + (hi - lo) / 2;
    if (better(levels[mid].price.raw, level.price.raw)) lo = mid + 1;
    else hi = mid;
  }
  if (lo < size && levels[lo].price == level.price) {
    if (level.quantity.raw == 0) {
      std::move(levels.begin() + static_cast<std::ptrdiff_t>(lo + 1),
                levels.begin() + static_cast<std::ptrdiff_t>(size),
                levels.begin() + static_cast<std::ptrdiff_t>(lo));
      --size;
    } else {
      levels[lo].quantity = level.quantity;
    }
    return true;
  }
  if (level.quantity.raw == 0) return true;
  if (size == levels.size()) {
    if (lo == size) return true;  // beyond the retained depth
    --size;                       // drop the farthest level
  }
  std::move_backward(levels.begin() + static_cast<std::ptrdiff_t>(lo),
                     levels.begin() + static_cast<std::ptrdiff_t>(size),
                     levels.begin() + static_cast<std::ptrdiff_t>(size + 1));
  levels[lo] = level;
  ++size;
  return true;
}

bool OrderBook::apply_snapshot(std::uint64_t last_update_id, std::span<const Level> bids,
                               std::span<const Level> asks, std::uint64_t receive_ns,
                               bool verified) noexcept {
  reset(BookState::syncing);
  if (bids.empty() || asks.empty()) {
    state_ = BookState::gap;
    return false;
  }
  for (const auto& level : bids) update(bids_, bid_size_, level, true);
  for (const auto& level : asks) update(asks_, ask_size_, level, false);
  last_update_id_ = last_update_id;
  last_receive_ns_ = receive_ns;
  if (!uncrossed()) {
    state_ = BookState::gap;
    return false;
  }
  state_ = verified ? BookState::verified : BookState::syncing;
  return true;
}

bool OrderBook::apply_changes(const DepthEvent& event) noexcept {
  for (std::size_t i = 0; i < event.bid_count; ++i) update(bids_, bid_size_, event.bids[i], true);
  for (std::size_t i = 0; i < event.ask_count; ++i) update(asks_, ask_size_, event.asks[i], false);
  last_update_id_ = event.final_id;
  last_receive_ns_ = event.receive_ns;
  event_ms_ = event.event_ms;
  state_ = uncrossed() ? BookState::verified : BookState::gap;
  return state_ == BookState::verified;
}

bool OrderBook::apply_binance(const DepthEvent& event) noexcept {
  if (state_ == BookState::empty || state_ == BookState::gap) return false;
  if (event.final_id <= last_update_id_) return true;
  const bool first_after_snapshot = state_ == BookState::syncing;
  // USD-M futures: the first event after a snapshot spans lastUpdateId (U <= id <= u)
  // or chains from it via pu; afterwards every event chains via pu.
  const bool contiguous =
      first_after_snapshot
          ? (event.first_id <= last_update_id_ + 1 && event.final_id >= last_update_id_ + 1) ||
                event.previous_final_id == last_update_id_
          : event.previous_final_id == last_update_id_;
  if (!contiguous) {
    state_ = BookState::gap;
    return false;
  }
  return apply_changes(event);
}

bool OrderBook::apply_bybit(const DepthEvent& event) noexcept {
  if (event.snapshot) {
    const auto ok = apply_snapshot(event.final_id, {event.bids.data(), event.bid_count},
                                   {event.asks.data(), event.ask_count}, event.receive_ns, true);
    event_ms_ = event.event_ms;
    return ok;
  }
  if (state_ != BookState::verified) return false;
  if (event.final_id != last_update_id_ + 1) {
    state_ = BookState::gap;
    return false;
  }
  return apply_changes(event);
}

void OrderBook::reset(BookState state) noexcept {
  bid_size_ = 0;
  ask_size_ = 0;
  last_update_id_ = 0;
  event_ms_ = 0;
  state_ = state;
}

Level OrderBook::best_bid() const noexcept { return bid_size_ == 0 ? Level{} : bids_[0]; }
Level OrderBook::best_ask() const noexcept { return ask_size_ == 0 ? Level{} : asks_[0]; }

Price OrderBook::mid() const noexcept {
  if (bid_size_ == 0 || ask_size_ == 0) return {};
  return Price{(bids_[0].price.raw + asks_[0].price.raw) / 2};
}

bool OrderBook::uncrossed() const noexcept {
  return bid_size_ > 0 && ask_size_ > 0 && bids_[0].price.raw > 0 &&
         bids_[0].price.raw < asks_[0].price.raw;
}

SweepResult OrderBook::sweep(Side taker_side, Quantity quantity, Price limit,
                             std::int64_t available_ppm) const noexcept {
  SweepResult result;
  const auto& levels = taker_side == Side::buy ? asks_ : bids_;
  const auto size = taker_side == Side::buy ? ask_size_ : bid_size_;
  std::int64_t remaining = quantity.raw;
  for (std::size_t i = 0; i < size && remaining > 0; ++i) {
    const auto& level = levels[i];
    if (limit.raw > 0) {
      if (taker_side == Side::buy && level.price.raw > limit.raw) break;
      if (taker_side == Side::sell && level.price.raw < limit.raw) break;
    }
    const auto available = apply_ppm(level.quantity.raw, available_ppm);
    const auto take = std::min(available, remaining);
    if (take <= 0) continue;
    remaining -= take;
    result.filled.raw += take;
    result.notional_raw += mul_scaled(level.price.raw, take);
    result.worst = level.price;
  }
  if (result.filled.raw > 0) result.vwap = Price{div_scaled(result.notional_raw, result.filled.raw)};
  return result;
}

std::uint64_t OrderBook::state_hash() const noexcept {
  std::uint64_t hash = 1469598103934665603ULL;
  const auto mix = [&hash](std::uint64_t value) {
    hash ^= value;
    hash *= 1099511628211ULL;
  };
  mix(last_update_id_);
  mix(static_cast<std::uint64_t>(state_));
  const auto n_bids = std::min<std::size_t>(bid_size_, 32);
  const auto n_asks = std::min<std::size_t>(ask_size_, 32);
  for (std::size_t i = 0; i < n_bids; ++i) {
    mix(static_cast<std::uint64_t>(bids_[i].price.raw));
    mix(static_cast<std::uint64_t>(bids_[i].quantity.raw));
  }
  for (std::size_t i = 0; i < n_asks; ++i) {
    mix(static_cast<std::uint64_t>(asks_[i].price.raw));
    mix(static_cast<std::uint64_t>(asks_[i].quantity.raw));
  }
  return hash;
}

}  // namespace cxa
