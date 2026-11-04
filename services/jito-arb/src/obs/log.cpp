#include "obs/log.hpp"

#include <ctime>

namespace arb::obs {

Logger& Logger::get() {
  static Logger l;
  return l;
}

Level parse_level(const std::string& s) {
  if (s == "debug") return Level::debug;
  if (s == "warn") return Level::warn;
  if (s == "error") return Level::error;
  return Level::info;
}

void Logger::start(const std::string& path, Level min_level) {
  std::lock_guard<std::mutex> g(mu_);
  min_.store(int(min_level));
  if (!path.empty()) file_ = std::fopen(path.c_str(), "a");
  if (running_) return;
  running_ = true;
  stopping_ = false;
  th_ = std::thread([this] { loop(); });
}

void Logger::stop() {
  {
    std::lock_guard<std::mutex> g(mu_);
    if (!running_) return;
    stopping_ = true;
  }
  cv_.notify_all();
  th_.join();
  std::lock_guard<std::mutex> g(mu_);
  running_ = false;
  if (file_) std::fclose(file_);
  file_ = nullptr;
}

void Logger::write(Level l, const char* fmt, ...) {
  static const char* names[] = {"DBG", "INF", "WRN", "ERR"};
  char body[1024];
  va_list ap;
  va_start(ap, fmt);
  std::vsnprintf(body, sizeof body, fmt, ap);
  va_end(ap);
  timespec ts;
  clock_gettime(CLOCK_REALTIME, &ts);
  tm t;
  gmtime_r(&ts.tv_sec, &t);
  char line[1200];
  std::snprintf(line, sizeof line, "%04d-%02d-%02dT%02d:%02d:%02d.%03ldZ %s %s\n", t.tm_year + 1900, t.tm_mon + 1, t.tm_mday,
                t.tm_hour, t.tm_min, t.tm_sec, ts.tv_nsec / 1'000'000, names[int(l)], body);
  std::unique_lock<std::mutex> g(mu_);
  if (!running_) {
    std::fputs(line, stderr);
    return;
  }
  if (q_.size() < 65536) q_.emplace_back(line);
  g.unlock();
  cv_.notify_one();
}

void Logger::loop() {
  std::unique_lock<std::mutex> g(mu_);
  for (;;) {
    cv_.wait(g, [this] { return stopping_ || !q_.empty(); });
    while (!q_.empty()) {
      std::string s = std::move(q_.front());
      q_.pop_front();
      g.unlock();
      std::fputs(s.c_str(), stderr);
      if (file_) std::fputs(s.c_str(), file_);
      g.lock();
    }
    if (file_) std::fflush(file_);
    if (stopping_) return;
  }
}

}  // namespace arb::obs
