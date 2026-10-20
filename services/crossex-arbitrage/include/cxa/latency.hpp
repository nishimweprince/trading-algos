#pragma once

#include "cxa/config.hpp"

#include <algorithm>
#include <array>
#include <cstdint>

namespace cxa {

// One-way order latency for a venue, derived from measured signed-request RTTs.
// Deterministic: draws use a seeded xorshift so replay reproduces the same fills.
class LatencyModel final {
 public:
  static constexpr std::size_t kSamples = 256;

  LatencyModel() = default;
  LatencyModel(LatencyMode mode, std::uint64_t fixed_one_way_ns, std::uint64_t penalty_ns,
               std::uint64_t seed) noexcept
      : mode_(mode), fixed_ns_(fixed_one_way_ns), penalty_ns_(penalty_ns), state_(seed | 1U) {}

  void add_rtt_sample(std::uint64_t rtt_ns) noexcept {
    samples_[head_] = rtt_ns / 2;
    head_ = (head_ + 1) % kSamples;
    if (count_ < kSamples) ++count_;
    p95_ns_ = percentile(0.95);
  }

  [[nodiscard]] std::uint64_t draw() noexcept {
    if (count_ == 0 || mode_ == LatencyMode::fixed) return fixed_ns_ + penalty_ns_;
    if (mode_ == LatencyMode::pessimistic) return p95_ns_ + penalty_ns_;
    state_ ^= state_ << 13;
    state_ ^= state_ >> 7;
    state_ ^= state_ << 17;
    return samples_[state_ % count_] + penalty_ns_;
  }

  // One-way percentile of the retained samples (fixed value when empty).
  [[nodiscard]] std::uint64_t percentile(double p) const noexcept {
    if (count_ == 0) return fixed_ns_;
    std::array<std::uint64_t, kSamples> copy{};
    std::copy_n(samples_.begin(), count_, copy.begin());
    const auto n = static_cast<std::ptrdiff_t>(count_);
    const auto k = static_cast<std::ptrdiff_t>(p * static_cast<double>(count_ - 1));
    std::nth_element(copy.begin(), copy.begin() + k, copy.begin() + n);
    return copy[static_cast<std::size_t>(k)];
  }

  [[nodiscard]] std::size_t samples() const noexcept { return count_; }

 private:
  LatencyMode mode_{LatencyMode::fixed};
  std::uint64_t fixed_ns_{5'000'000};
  std::uint64_t penalty_ns_{};
  std::uint64_t state_{0x9E3779B97F4A7C15ULL};
  std::array<std::uint64_t, kSamples> samples_{};
  std::size_t head_{};
  std::size_t count_{};
  std::uint64_t p95_ns_{};
};

}  // namespace cxa
