#pragma once
// Small async logger (stand-in for spdlog, which is not installed here): callers
// format into a std::string and enqueue; one background thread writes to stderr
// and an optional file. Never logs key material.
#include <atomic>
#include <condition_variable>
#include <cstdarg>
#include <cstdio>
#include <deque>
#include <mutex>
#include <string>
#include <thread>

namespace arb::obs {

enum class Level { debug = 0, info = 1, warn = 2, error = 3 };

class Logger {
 public:
  static Logger& get();
  void start(const std::string& path, Level min_level);
  void stop();
  void set_level(Level l) { min_.store(int(l)); }
  bool enabled(Level l) const { return int(l) >= min_.load(std::memory_order_relaxed); }
  void write(Level l, const char* fmt, ...) __attribute__((format(printf, 3, 4)));

 private:
  Logger() = default;
  void loop();
  std::atomic<int> min_{int(Level::info)};
  std::mutex mu_;
  std::condition_variable cv_;
  std::deque<std::string> q_;
  std::thread th_;
  bool running_ = false, stopping_ = false;
  FILE* file_ = nullptr;
};

Level parse_level(const std::string& s);

}  // namespace arb::obs

#define ARB_LOG(lvl, ...)                                                                   \
  do {                                                                                      \
    if (::arb::obs::Logger::get().enabled(lvl)) ::arb::obs::Logger::get().write(lvl, __VA_ARGS__); \
  } while (0)
#define LOG_DEBUG(...) ARB_LOG(::arb::obs::Level::debug, __VA_ARGS__)
#define LOG_INFO(...) ARB_LOG(::arb::obs::Level::info, __VA_ARGS__)
#define LOG_WARN(...) ARB_LOG(::arb::obs::Level::warn, __VA_ARGS__)
#define LOG_ERROR(...) ARB_LOG(::arb::obs::Level::error, __VA_ARGS__)
