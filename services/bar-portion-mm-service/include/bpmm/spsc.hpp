#pragma once

#include <array>
#include <atomic>
#include <cstddef>
#include <utility>
#include <type_traits>

namespace bpmm {

template <typename T, std::size_t Capacity>
class SpscRing final {
  static_assert(Capacity >= 2);

 public:
  bool try_push(T value) noexcept(std::is_nothrow_move_assignable_v<T>) {
    const auto head = head_.load(std::memory_order_relaxed);
    const auto next = (head + 1) % Capacity;
    if (next == tail_.load(std::memory_order_acquire)) return false;
    values_[head] = std::move(value);
    head_.store(next, std::memory_order_release);
    return true;
  }

  bool try_pop(T& output) noexcept(std::is_nothrow_move_assignable_v<T>) {
    const auto tail = tail_.load(std::memory_order_relaxed);
    if (tail == head_.load(std::memory_order_acquire)) return false;
    output = std::move(values_[tail]);
    tail_.store((tail + 1) % Capacity, std::memory_order_release);
    return true;
  }

  [[nodiscard]] bool empty() const noexcept {
    return tail_.load(std::memory_order_acquire) == head_.load(std::memory_order_acquire);
  }

 private:
  std::array<T, Capacity> values_{};
  alignas(64) std::atomic<std::size_t> head_{0};
  alignas(64) std::atomic<std::size_t> tail_{0};
};

}  // namespace bpmm
