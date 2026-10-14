#include "bpmm/book.hpp"

#include <algorithm>

namespace bpmm {

void OrderBook::sort(Levels& levels, std::size_t size, bool descending) noexcept {
  std::sort(levels.begin(), levels.begin() + static_cast<std::ptrdiff_t>(size),
            [descending](const Level& lhs, const Level& rhs) {
              return descending ? lhs.price.raw > rhs.price.raw : lhs.price.raw < rhs.price.raw;
            });
}

bool OrderBook::update(Levels& levels, std::size_t& size, Level level, bool descending) noexcept {
  for (std::size_t i = 0; i < size; ++i) {
    if (levels[i].price == level.price) {
      if (level.quantity.raw == 0) {
        for (std::size_t j = i + 1; j < size; ++j) levels[j - 1] = levels[j];
        --size;
      } else {
        levels[i].quantity = level.quantity;
      }
      return true;
    }
  }
  if (level.quantity.raw == 0) return true;
  if (size == levels.size()) return false;
  levels[size++] = level;
  sort(levels, size, descending);
  return true;
}

bool OrderBook::apply_snapshot(std::uint64_t last_update_id, std::span<const Level> bids,
                               std::span<const Level> asks) noexcept {
  reset(BookState::syncing);
  if (bids.empty() || asks.empty() || bids.size() > kCapacity || asks.size() > kCapacity) return false;
  for (const auto& level : bids) {
    if (level.quantity.raw > 0) bids_[bid_size_++] = level;
  }
  for (const auto& level : asks) {
    if (level.quantity.raw > 0) asks_[ask_size_++] = level;
  }
  sort(bids_, bid_size_, true);
  sort(asks_, ask_size_, false);
  last_update_id_ = last_update_id;
  state_ = uncrossed() ? BookState::syncing : BookState::gap;
  return state_ != BookState::gap;
}

bool OrderBook::apply(const DepthEvent& event) noexcept {
  if (state_ == BookState::empty || state_ == BookState::gap) return false;
  if (event.final_id <= last_update_id_) return true;
  const bool first_after_snapshot = state_ == BookState::syncing;
  const bool contiguous = first_after_snapshot
                              ? event.first_id <= last_update_id_ + 1 && event.final_id >= last_update_id_ + 1
                              : event.previous_final_id == last_update_id_;
  if (!contiguous) {
    state_ = BookState::gap;
    return false;
  }
  for (std::size_t i = 0; i < event.bid_count; ++i) {
    if (!update(bids_, bid_size_, event.bids[i], true)) {
      state_ = BookState::gap;
      return false;
    }
  }
  for (std::size_t i = 0; i < event.ask_count; ++i) {
    if (!update(asks_, ask_size_, event.asks[i], false)) {
      state_ = BookState::gap;
      return false;
    }
  }
  last_update_id_ = event.final_id;
  state_ = uncrossed() ? BookState::verified : BookState::gap;
  return state_ == BookState::verified;
}

void OrderBook::reset(BookState state) noexcept {
  bid_size_ = 0;
  ask_size_ = 0;
  last_update_id_ = 0;
  state_ = state;
}

Level OrderBook::best_bid() const noexcept { return bid_size_ == 0 ? Level{} : bids_[0]; }
Level OrderBook::best_ask() const noexcept { return ask_size_ == 0 ? Level{} : asks_[0]; }

Quantity OrderBook::quantity_at(Side side, Price price) const noexcept {
  const auto& levels = side == Side::buy ? bids_ : asks_;
  const auto size = side == Side::buy ? bid_size_ : ask_size_;
  for (std::size_t i = 0; i < size; ++i) {
    if (levels[i].price == price) return levels[i].quantity;
  }
  return {};
}

bool OrderBook::uncrossed() const noexcept {
  return bid_size_ > 0 && ask_size_ > 0 && bids_[0].price.raw > 0 &&
         bids_[0].price.raw < asks_[0].price.raw;
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

}  // namespace bpmm
