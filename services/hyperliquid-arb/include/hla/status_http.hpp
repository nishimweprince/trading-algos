#pragma once

#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>

namespace hla {

// Loopback HTTP for operators / Cloudflare tunnels: "/" dashboard, "/api/summary",
// "/health". Updated from the decision thread via publish(); never blocks trading.
class StatusHttp final {
 public:
  explicit StatusHttp(std::uint16_t port);
  ~StatusHttp();
  StatusHttp(const StatusHttp&) = delete;
  StatusHttp& operator=(const StatusHttp&) = delete;

  bool start(std::string& error);
  void stop();
  void publish(std::string json);

 private:
  void serve();
  std::string snapshot() const;
  std::string dashboard() const;

  std::uint16_t port_{};
  std::atomic<bool> stop_{false};
  std::thread thread_;
  std::intptr_t socket_{-1};
  mutable std::mutex mutex_;
  std::string json_{"{\"live\":false,\"note\":\"starting\"}"};
};

}  // namespace hla
