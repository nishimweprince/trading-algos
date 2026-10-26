#pragma once

#include <atomic>
#include <cstdint>
#include <deque>
#include <mutex>
#include <string_view>
#include <vector>

namespace pkm::platform {

// Pins the calling thread to one core; false on failure.
bool pin_current_thread(unsigned core) noexcept;
// SIGINT / SIGTERM set `stop`; SIGPIPE is ignored.
void install_stop_handler(std::atomic<bool>& stop) noexcept;
// "[pkm 2026-10-26T12:00:00.123Z] text" to stderr (journald keeps it).
void log(std::string_view text) noexcept;

}  // namespace pkm::platform

namespace pkm {

// Many producers, one consumer. The hot thread drains with `try_drain`, which
// never waits: if a producer holds the lock it simply drains next loop.
template <typename T>
class Mailbox final {
 public:
  void push(T value) {
    std::lock_guard<std::mutex> lock(mutex_);
    queue_.push_back(std::move(value));
  }
  bool try_drain(std::vector<T>& out) {
    std::unique_lock<std::mutex> lock(mutex_, std::try_to_lock);
    if (!lock.owns_lock() || queue_.empty()) return false;
    for (auto& v : queue_) out.push_back(std::move(v));
    queue_.clear();
    return true;
  }
  void drain(std::vector<T>& out) {
    std::lock_guard<std::mutex> lock(mutex_);
    for (auto& v : queue_) out.push_back(std::move(v));
    queue_.clear();
  }

 private:
  std::mutex mutex_;
  std::deque<T> queue_;
};

}  // namespace pkm
