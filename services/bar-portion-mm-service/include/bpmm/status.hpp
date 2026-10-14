#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <string>
#include <thread>

namespace bpmm {

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

}  // namespace bpmm
