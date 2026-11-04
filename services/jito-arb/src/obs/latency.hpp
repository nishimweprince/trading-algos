#pragma once
// Rolling latency window: samples from the last `window_s` seconds, with percentiles computed on demand
// (once a second by the metrics tick, never on the hot path). Single-threaded: the feed thread owns it.
#include <algorithm>
#include <cstdint>
#include <deque>
#include <vector>

namespace arb::obs {

struct LatencyStats {
  int64_t p50 = -1, p90 = -1, p99 = -1, max = -1;  // -1 = no samples
  uint64_t count = 0;
};

class LatencyWindow {
 public:
  explicit LatencyWindow(uint64_t window_s = 60, size_t max_samples = 200'000)
      : window_ns_(window_s * 1'000'000'000ULL), max_(max_samples) {}
  void add(uint64_t now_ns, int64_t value) {
    q_.push_back({now_ns, value});
    if (q_.size() > max_) q_.pop_front();
    ++total_;
  }
  LatencyStats stats(uint64_t now_ns) {
    while (!q_.empty() && now_ns > q_.front().t && now_ns - q_.front().t > window_ns_) q_.pop_front();
    LatencyStats s;
    s.count = q_.size();
    if (q_.empty()) return s;
    std::vector<int64_t> v;
    v.reserve(q_.size());
    for (auto& e : q_) v.push_back(e.v);
    auto at = [&](double p) {
      size_t k = size_t(p * double(v.size() - 1));
      std::nth_element(v.begin(), v.begin() + long(k), v.end());
      return v[k];
    };
    s.p50 = at(0.50);
    s.p90 = at(0.90);
    s.p99 = at(0.99);
    s.max = *std::max_element(v.begin(), v.end());
    return s;
  }
  uint64_t total() const { return total_; }

 private:
  struct E {
    uint64_t t;
    int64_t v;
  };
  uint64_t window_ns_;
  size_t max_;
  std::deque<E> q_;
  uint64_t total_ = 0;
};

}  // namespace arb::obs
