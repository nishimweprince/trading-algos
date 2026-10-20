#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <string>
#include <thread>

namespace cxa {

// Loopback-only HTTP: "/" dashboard, "/api/summary" (+ health aliases), "/api/activity"
// (recent cycles and decisions from the journal tail). Never touches the decision thread.
class StatusServer final {
 public:
  using Snapshot = std::function<std::string()>;
  StatusServer(std::uint16_t port, Snapshot snapshot, std::string journal_path = {});
  ~StatusServer();
  bool start(std::string& error);
  void stop();

 private:
  void serve();
  std::uint16_t port_;
  Snapshot snapshot_;
  std::string journal_path_;
  std::atomic<bool> stop_{false};
  std::thread thread_;
  int socket_{-1};
};

std::string dashboard_page();
std::string activity_json(const std::string& journal_path);

}  // namespace cxa
