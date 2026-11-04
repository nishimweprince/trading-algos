#pragma once
// Prometheus text exposition on 127.0.0.1:9100 (stand-in for prometheus-cpp).
// Counters and gauges are lock-free atomics registered once at boot; the HTTP
// thread renders them on GET /metrics.
#include <atomic>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace arb::obs {

struct Counter {
  std::atomic<uint64_t> v{0};
  void inc(uint64_t n = 1) { v.fetch_add(n, std::memory_order_relaxed); }
};

struct Gauge {
  std::atomic<int64_t> v{0};
  void set(int64_t x) { v.store(x, std::memory_order_relaxed); }
};

/** Fixed-bucket histogram (upper bounds inclusive, like Prometheus `le`). */
struct Histogram {
  explicit Histogram(std::vector<double> bounds);
  void observe(double x);
  std::vector<double> bounds;
  std::unique_ptr<std::atomic<uint64_t>[]> buckets;  // bounds.size() + 1 (+Inf)
  std::atomic<uint64_t> count{0};
  std::atomic<uint64_t> sum_milli{0};
};

class Registry {
 public:
  static Registry& get();
  Counter& counter(const std::string& name, const std::string& help);
  Gauge& gauge(const std::string& name, const std::string& help);
  Histogram& histogram(const std::string& name, const std::string& help, std::vector<double> bounds);
  std::string render() const;

 private:
  struct Entry {
    std::string name, help, type;
    std::unique_ptr<Counter> c;
    std::unique_ptr<Gauge> g;
    std::unique_ptr<Histogram> h;
  };
  mutable std::mutex mu_;
  std::vector<std::unique_ptr<Entry>> entries_;
};

/** Minimal blocking HTTP/1.0 server for GET /metrics. */
class MetricsServer {
 public:
  ~MetricsServer();
  bool start(const std::string& addr, int port, std::string* err);
  void stop();
  int port() const { return port_; }

 private:
  void loop();
  int fd_ = -1;
  int port_ = 0;
  std::atomic<bool> stop_{false};
  std::thread th_;
};

}  // namespace arb::obs
