#include "pkm/book.hpp"

#include <bit>

namespace pkm {

void Book::Levels::put(int key, double value) noexcept {
  if (key < 0 || key >= kKeys) return;
  const auto k = static_cast<std::size_t>(key);
  const std::uint64_t bit = 1ULL << (k % 64);
  const bool had = (used[k / 64] & bit) != 0;
  if (value <= 0.0) {
    if (had) {
      used[k / 64] &= ~bit;
      size[k] = 0;
      --count;
    }
    return;
  }
  size[k] = value;
  if (!had) {
    used[k / 64] |= bit;
    ++count;
  }
}

void Book::Levels::clear() noexcept {
  if (count == 0) return;
  for (std::size_t w = 0; w < used.size(); ++w) {
    std::uint64_t bits = used[w];
    while (bits != 0) {
      const int b = std::countr_zero(bits);
      size[w * 64 + static_cast<std::size_t>(b)] = 0;
      bits &= bits - 1;
    }
    used[w] = 0;
  }
  count = 0;
}

int Book::Levels::highest() const noexcept {
  if (count == 0) return -1;
  for (std::size_t w = used.size(); w-- > 0;)
    if (used[w] != 0) return static_cast<int>(w * 64) + 63 - std::countl_zero(used[w]);
  return -1;
}

int Book::Levels::lowest() const noexcept {
  if (count == 0) return -1;
  for (std::size_t w = 0; w < used.size(); ++w)
    if (used[w] != 0) return static_cast<int>(w * 64) + std::countr_zero(used[w]);
  return -1;
}

void Book::begin_snapshot(std::int64_t recv_ns) noexcept {
  bids_.clear();
  asks_.clear();
  snapshot_ns_ = recv_ns == 0 ? 1 : recv_ns;
  last_update_ns_ = recv_ns;
}

void Book::set(Side side, int key, double size, std::int64_t recv_ns) noexcept {
  levels(side).put(key, round_size(size));
  last_update_ns_ = recv_ns;
}

void Book::add(Side side, int key, double delta, std::int64_t recv_ns) noexcept {
  set(side, key, size_at(side, key) + delta, recv_ns);
}

void Book::invalidate() noexcept {
  bids_.clear();
  asks_.clear();
  snapshot_ns_ = 0;
}

int Book::best_bid_key() const noexcept { return bids_.highest(); }
int Book::best_ask_key() const noexcept { return asks_.lowest(); }

std::optional<double> Book::best_bid() const noexcept {
  const int k = best_bid_key();
  return k < 0 ? std::nullopt : std::optional<double>(key_px(k));
}

std::optional<double> Book::best_ask() const noexcept {
  const int k = best_ask_key();
  return k < 0 ? std::nullopt : std::optional<double>(key_px(k));
}

double Book::size_at(Side side, int key) const noexcept {
  if (key < 0 || key >= kKeys) return 0;
  return (side == Side::bid ? bids_ : asks_).size[static_cast<std::size_t>(key)];
}

Quote Book::quote() const noexcept {
  if (!has_snapshot()) return {};
  const int b = best_bid_key();
  const int a = best_ask_key();
  if (b < 0 || a < 0 || b >= a) return {};
  return {true, key_px(b), key_px(a)};
}

}  // namespace pkm
